#!/usr/bin/env python3
"""Call Tencent Cloud Yuanbao Search with a protected local credential."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.request

ENDPOINT = "https://api.wsa.cloud.tencent.com/SearchPro"
ENV_KEY = "TENCENTCLOUD_WSA_APIKEY"
KEY_FILE = Path.home() / ".codex" / "secrets" / "yuanbao-search.dpapi"


def _windows_dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI 凭据存储仅支持 Windows；请设置环境变量。")

    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    data_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), ctypes.cast(data_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def load_key() -> str:
    value = os.environ.get(ENV_KEY, "").strip()
    if value:
        return value
    try:
        return _windows_dpapi(KEY_FILE.read_bytes(), protect=False).decode("utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"没有找到元宝搜索密钥；请设置 {ENV_KEY}，或在 Windows 中运行 --store-key。") from exc
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("无法解密本地密钥，请确认使用的是保存该密钥的 Windows 账户。") from exc


def store_key() -> None:
    if os.name != "nt":
        raise RuntimeError("本地加密存储仅支持 Windows；请使用环境变量。")
    key = getpass.getpass("元宝搜索 API Key（输入时不显示）: ").strip()
    if not key or not key.startswith("sk-") or len(key) > 512:
        raise ValueError("密钥格式无效。")
    encrypted = _windows_dpapi(key.encode("utf-8"), protect=True)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="yuanbao-", suffix=".dpapi", dir=KEY_FILE.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(encrypted)
        os.replace(temp_name, KEY_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    print("密钥已保存到当前 Windows 用户的加密存储。")


def freshness_value(value: str) -> str:
    match = re.fullmatch(r"([dmy])(\d*)", value)
    if match:
        unit, amount = match.groups()
        maximum = {"d": 30, "m": 12, "y": 5}[unit]
        if not amount or 1 <= int(amount) <= maximum:
            return value
    else:
        parts = value.split(",")
        if len(parts) in (1, 2):
            try:
                dates = [dt.date.fromisoformat(part) for part in parts]
                if all(d >= dt.date(1970, 1, 1) and d <= dt.date.today() for d in dates):
                    return value
            except ValueError:
                pass
    raise argparse.ArgumentTypeError("时间范围应为 d1-d30、m1-m12、y1-y5 或合法日期/日期范围。")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_response(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("服务响应不是 JSON 对象。")
    response = body.get("Response")
    if not isinstance(response, dict):
        raise ValueError("服务响应中缺少 Response 对象。")
    error = response.get("Error")
    if isinstance(error, dict):
        raise RuntimeError(f"服务错误 {error.get('Code', 'Unknown')}：{error.get('Message', '')}；请求 ID：{response.get('RequestId', '无')}")
    pages = response.get("Pages", [])
    if not isinstance(pages, list):
        raise ValueError("服务响应中的 Pages 格式不正确。")
    results = []
    for page in pages:
        if isinstance(page, str):
            try:
                page = json.loads(page)
            except json.JSONDecodeError:
                page = {"unparsed": page}
        results.append(page)
    return {
        "query": response.get("Query"),
        "version": response.get("Version"),
        "message": response.get("Msg"),
        "request_id": response.get("RequestId"),
        "result_count": len(results),
        "results": results,
    }


def compact_results(result: dict) -> dict:
    """Keep source details and short excerpts; --full retains all provider fields."""
    fields = ("title", "url", "date", "site", "score", "authority_level", "passage")
    compact = []
    for page in result["results"]:
        if not isinstance(page, dict):
            compact.append(page)
            continue
        item = {field: page[field] for field in fields if field in page}
        if "unparsed" in page:
            item["unparsed"] = page["unparsed"]
        passage = item.get("passage")
        if isinstance(passage, str) and len(passage) > 700:
            item["passage"] = passage[:700] + "…"
            item["passage_truncated"] = True
        compact.append(item)
    return {**result, "results": compact}


def search(payload: dict, key: str) -> dict:
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"请求未成功（HTTP {exc.code}）。请检查服务状态及密钥。") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"无法连接元宝搜索服务（{type(exc).__name__}）。") from None
    try:
        return normalize_response(body)
    except (RuntimeError, ValueError) as exc:
        raise type(exc)(str(exc).replace(key, "[REDACTED]")) from None


def main() -> int:
    parser = argparse.ArgumentParser(description="使用腾讯云元宝搜索 API。")
    parser.add_argument("query", nargs="?", help="搜索关键词；不要包含密钥或无关私密信息")
    parser.add_argument("--site", help="限定或排除站点，按腾讯云 Site 参数格式填写")
    parser.add_argument("--freshness", type=freshness_value, help="例如 d7、m3、y1 或日期范围")
    parser.add_argument("--industry", choices=["gov", "news", "acad", "finance"])
    parser.add_argument("--count", type=int, choices=[10, 20, 30, 40, 50])
    parser.add_argument("--mode", type=int, choices=[0, 1, 2])
    parser.add_argument("--full", action="store_true", help="返回完整结果字段及正文摘要")
    parser.add_argument("--store-key", action="store_true", help="交互式保存或替换当前 Windows 用户加密密钥")
    args = parser.parse_args()
    if args.store_key:
        if args.query:
            parser.error("--store-key 不与搜索词同时使用。")
        store_key()
        return 0
    if not args.query or not args.query.strip() or len(args.query.encode("utf-8")) > 1500:
        parser.error("搜索词不能为空，且 UTF-8 编码不能超过 1500 字节。")
    payload = {"Query": args.query.strip()}
    for attribute, field in (("site", "Site"), ("freshness", "Freshness"), ("industry", "Industry"), ("count", "Cnt"), ("mode", "Mode")):
        value = getattr(args, attribute)
        if value is not None:
            payload[field] = value
    key = load_key()
    if not key:
        raise RuntimeError("元宝搜索密钥为空，请重新配置。")
    result = search(payload, key)
    if not args.full:
        result = compact_results(result)
    rendered = json.dumps(result, ensure_ascii=False, indent=2).replace(key, "[REDACTED]")
    print(rendered)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"元宝搜索失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
