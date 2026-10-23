# main.py 主逻辑：包括字段拼接、模拟请求
import json
import os
import time
import random
import logging
import hashlib
import tempfile
import requests
import urllib.parse
from push import push
from log_utils import setup_logging
from config import data, headers, cookies, READ_NUM, PUSH_METHOD, book, chapter, CURL_FILE

# 阅读次数在 ±20% 范围内随机
READ_NUM = random.randint(int(READ_NUM * 0.8), int(READ_NUM * 1.2))

# 加密盐及其它默认值
KEY = "3c5c8717f3daf09iop3423zafeqoi"
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"
COOKIE_DATA_VARIANTS = [{"rq": "%2Fweb%2Fbook%2Fread", "ql": False},{"rq": "%2Fweb%2Fbook%2Fread", "ql": True},{"rq": "%2Fweb%2Fbook%2Fread"},]
REQUEST_TIMEOUT = (10, 30)
# 续期整体重试轮数（每轮遍历全部 payload 变体，轮间随机退避，防止瞬态抖动导致整轮失败）
RENEW_MAX_ROUNDS = 3
# 无 synckey 时连续修复的最大次数，防止死循环
NO_SYNCKEY_MAX_RETRY = 5
# 阅读请求连续失败的最大重试次数，超过则推送失败并终止
READ_MAX_RETRY = 5
AUTH_ERROR_CODES = {-2012, -2013}

# 续期后最新的完整 cookie（含未截断的 wr_skey），用于持久化及回写 GitHub Secret
NEW_CURL_FILE = CURL_FILE
latest_cookies = dict(cookies)


def encode_data(data):
    """数据编码"""
    return '&'.join(f"{k}={urllib.parse.quote(str(data[k]), safe='')}" for k in sorted(data.keys()))


def cal_hash(input_string):
    """计算哈希值"""
    _7032f5 = 0x15051505
    _cc1055 = _7032f5
    length = len(input_string)
    _19094e = length - 1

    while _19094e > 0:
        _7032f5 = 0x7fffffff & (_7032f5 ^ ord(input_string[_19094e]) << (length - _19094e) % 30)
        _cc1055 = 0x7fffffff & (_cc1055 ^ ord(input_string[_19094e - 1]) << _19094e % 30)
        _19094e -= 2

    return hex(_7032f5 + _cc1055)[2:].lower()

def get_wr_skey():
    """刷新cookie密钥：每轮遍历全部 payload 变体，失败时按轮退避重试"""
    for round_no in range(1, RENEW_MAX_ROUNDS + 1):
        for cookie_data in COOKIE_DATA_VARIANTS:
            try:
                response = requests.post(
                    RENEW_URL,
                    headers=headers,
                    cookies=cookies,
                    data=json.dumps(cookie_data, separators=(',', ':')),
                    timeout=REQUEST_TIMEOUT,
                )

                if 'wr_skey' in response.cookies:
                    latest_cookies.update(dict(response.cookies))
                    return response.cookies['wr_skey'][:8]
                else:
                    continue
            except requests.RequestException as exc:
                logging.warning(f"refresh_cookie 请求失败，payload={cookie_data}，原因：{exc}")
        if round_no < RENEW_MAX_ROUNDS:
            sleep_time = random.randint(30, 60)
            logging.warning(f"第 {round_no}/{RENEW_MAX_ROUNDS} 轮续期未拿到新密钥，{sleep_time} 秒后重试...")
            time.sleep(sleep_time)

    return None

def fix_no_synckey():
    """无 synckey 时尝试修复：尽力而为，失败仅记录日志，由调用方的重试上限兜底"""
    try:
        requests.post(FIX_SYNCKEY_URL, headers=headers, cookies=cookies, data=json.dumps({"bookIds":["3300060341"]}, separators=(',', ':')), timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logging.warning(f"fix_no_synckey 请求失败：{exc}")


def save_new_curl():
    """持久化续期后的完整 cURL，供下次任务读取及 Actions 回写 Secret"""
    cookie_str = '; '.join(f"{k}={v}" for k, v in latest_cookies.items())
    parts = [f"curl '{READ_URL}'"]
    parts += [f"-H '{k}: {v}'" for k, v in headers.items()]
    parts.append(f"-H 'Cookie: {cookie_str}'")
    curl_cmd = ' '.join(parts)

    try:
        with open(NEW_CURL_FILE, encoding='utf-8') as f:
            if f.read() == curl_cmd:
                return
    except OSError:
        pass

    directory = os.path.dirname(os.path.abspath(NEW_CURL_FILE))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix='.new_curl.', dir=directory, text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(curl_cmd)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, NEW_CURL_FILE)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
    logging.info("检测到新 cookie，已保存最新 cURL：%s", NEW_CURL_FILE)

refresh_print = setup_logging()

def refresh_cookie(strict=True):
    logging.info("刷新 cookie")
    new_skey = get_wr_skey()
    if new_skey:
        cookies['wr_skey'] = new_skey
        save_new_curl()
        logging.info(f"密钥刷新成功，新密钥：{new_skey[:2]}***")
        logging.info("重新本次阅读。")
        return True

    ERROR_CODE = "无法获取新密钥，当前登录状态可能已失效，终止运行。"
    if strict:
        logging.error(ERROR_CODE)
        push(ERROR_CODE, PUSH_METHOD, is_success=False)
        raise Exception(ERROR_CODE)

    logging.warning("启动时未获取到新密钥，保留现有 cookie 继续尝试阅读。")
    return False

# renewal 失败不等于当前阅读会话已失效；启动时刷新失败也继续尝试阅读。
refresh_cookie(strict=False)
index = 1
lastTime = int(time.time()) - 30
readSeconds = 0
no_synckey_count = 0
read_fail_count = 0
# 每次运行读两本书：前半程读一本，后半程换一本，章节各自从随机起点顺序往后读
half = (READ_NUM + 1) // 2
current_books = random.sample(book, 2)
chapter_starts = [random.randint(0, len(chapter) - 1) for _ in range(2)]
logging.info(f"一共需要阅读 {READ_NUM} 次。")

while index <= READ_NUM:
    data.pop('s')
    seg = 0 if index <= half else 1
    data['b'] = current_books[seg]
    data['c'] = chapter[(chapter_starts[seg] + index - 1 - seg * half) % len(chapter)]
    thisTime = int(time.time())
    data['ct'] = thisTime
    data['rt'] = thisTime - lastTime
    data['ts'] = int(thisTime * 1000) + random.randint(0, 1000)
    data['rn'] = random.randint(0, 1000)
    data['sg'] = hashlib.sha256(f"{data['ts']}{data['rn']}{KEY}".encode()).hexdigest()
    data['s'] = cal_hash(encode_data(data))

    refresh_print(f"阅读进度: 第 {index}/{READ_NUM} 次，已完成 {readSeconds / 60:.1f} 分钟")
    logging.debug("data: %s", data)
    try:
        response = requests.post(
            READ_URL,
            headers=headers,
            cookies=cookies,
            data=json.dumps(data, separators=(',', ':')),
            timeout=REQUEST_TIMEOUT,
        )
        resData = response.json()
    except ValueError as exc:
        ERROR_CODE = f"read 接口返回非 JSON 响应，HTTP {response.status_code}。"
        logging.error(ERROR_CODE)
        push(ERROR_CODE, PUSH_METHOD, is_success=False)
        raise RuntimeError(ERROR_CODE) from exc
    except requests.RequestException as exc:
        # 网络瞬态异常：有限次退避重试，连续失败则推送失败并终止
        read_fail_count += 1
        if read_fail_count > READ_MAX_RETRY:
            ERROR_CODE = f"阅读请求重试 {READ_MAX_RETRY} 次后仍失败，终止运行。原因：{exc}"
            logging.error(ERROR_CODE)
            push(ERROR_CODE, PUSH_METHOD, is_success=False)
            raise Exception(ERROR_CODE)
        sleep_time = random.randint(30, 60)
        logging.warning(f"阅读请求失败（第 {read_fail_count}/{READ_MAX_RETRY} 次）：{exc}，{sleep_time} 秒后重试...")
        time.sleep(sleep_time)
        continue
    read_fail_count = 0

    logging.debug("response: %s", resData)

    if 'succ' in resData:
        if 'synckey' in resData:
            no_synckey_count = 0
            readSeconds += data['rt']
            lastTime = thisTime
            index += 1
            time.sleep(random.randint(28, 45))
            refresh_print(f"阅读进度: 第 {min(index, READ_NUM + 1) - 1}/{READ_NUM} 次，已完成 {readSeconds / 60:.1f} 分钟")
        else:
            no_synckey_count += 1
            if no_synckey_count > NO_SYNCKEY_MAX_RETRY:
                ERROR_CODE = f"连续 {NO_SYNCKEY_MAX_RETRY} 次修复仍无 synckey，终止运行。"
                logging.error(ERROR_CODE)
                push(ERROR_CODE, PUSH_METHOD, is_success=False)
                raise Exception(ERROR_CODE)
            logging.warning(f"无 synckey（第 {no_synckey_count}/{NO_SYNCKEY_MAX_RETRY} 次），尝试修复...")
            fix_no_synckey()
            time.sleep(random.randint(28, 45))
    else:
        err_code = resData.get('errCode')
        err_msg = resData.get('errMsg')
        logging.warning(
            "read 接口返回异常：HTTP %s，errCode=%s，errMsg=%s",
            response.status_code,
            err_code,
            err_msg,
        )

        if err_code in AUTH_ERROR_CODES:
            logging.warning("检测到登录/鉴权异常，尝试刷新 cookie...")
        else:
            logging.warning("未识别为已知鉴权错误，保留现有刷新流程尝试恢复...")
        refresh_cookie()

logging.info("阅读脚本已完成。")

if PUSH_METHOD not in (None, ''):
    logging.info("开始推送...")
    push(f"微信读书自动阅读完成。\n阅读时长：{readSeconds / 60:.1f} 分钟。", PUSH_METHOD, is_success=True)
else:
    logging.info("未配置推送渠道，跳过推送。")
