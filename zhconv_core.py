"""
繁化姬長檔轉換核心：把長文字切段 → 逐段呼叫繁化姬 /convert API → 合併。
不依賴 GUI，可單獨 import 使用或寫成命令列腳本。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

import requests

API_URL = "https://api.zhconvert.org/convert"

CONVERTERS = {
    "台灣化": "Taiwan",
    "繁體化": "Traditional",
    "香港化": "Hongkong",
    "簡體化": "Simplified",
    "中國化": "China",
    "維基繁體化": "WikiTraditional",
    "維基簡體化": "WikiSimplified",
    "注音化": "Bopomofo",
    "拼音化": "Pinyin",
    "火星化": "Mars",
}

# 單行太長時，優先在這些字元後面斷開
SENTENCE_BREAKS = "。！？；…」』）\n!?;"
CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002ffff]")


class ConvertError(Exception):
    pass


class Cancelled(Exception):
    pass


class TooLong(ConvertError):
    """疑似因段落太長而失敗（413/502/504、逾時、回應不是 JSON），可切小重送。"""


SIZE_STATUS = (413, 502, 504)   # 這些狀態碼常見於請求太大或伺服器處理逾時
MIN_SPLIT = 500                 # 自動切小的下限，低於此長度就不再切


@dataclass
class ConvertOptions:
    converter: str = "Taiwan"
    chunk_size: int = 20000          # 每段最多字元數
    delay: float = 0.5               # 每次請求間隔（秒），避免對伺服器太兇
    retries: int = 4                 # 失敗重試次數（指數退避）
    timeout: float = 60
    api_key: str = ""
    modules: dict = field(default_factory=dict)     # {"Naruto": 1, "GanToZuo": 0}
    user_pre_replace: str = ""       # "搜尋=取代\n..."
    user_post_replace: str = ""
    user_protect_replace: str = ""   # 一行一個詞
    extra_params: dict = field(default_factory=dict)
    json_convert_keys: bool = False
    output_encoding: str = "utf-8"


# ---------------------------------------------------------------- 讀檔 --------

def read_text(path: str, encoding: str = "auto") -> tuple[str, str]:
    """回傳 (文字, 實際使用的編碼)。auto 會依序嘗試常見中文編碼。"""
    raw = open(path, "rb").read()
    if encoding != "auto":
        return raw.decode(encoding), encoding
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8 (有無法解碼字元)"


# ---------------------------------------------------------------- 切段 --------

def _split_long_line(line: str, size: int) -> list[str]:
    """一行超過 size 時，盡量在句尾標點處切開；真的沒標點才硬切。"""
    pieces, start = [], 0
    while len(line) - start > size:
        window = line[start:start + size]
        cut = max(window.rfind(ch) for ch in SENTENCE_BREAKS)
        if cut < size // 2:          # 找不到合適的標點，硬切
            cut = size - 1
        pieces.append(line[start:start + cut + 1])
        start += cut + 1
    pieces.append(line[start:])
    return pieces


def split_text(text: str, size: int) -> list[str]:
    """以「行」為單位打包成不超過 size 的段落，合併後與原文完全相同。"""
    if size < 100:
        raise ValueError("chunk_size 太小")
    chunks, buf, buf_len = [], [], 0
    for line in text.splitlines(keepends=True):
        parts = [line] if len(line) <= size else _split_long_line(line, size)
        for part in parts:
            if buf_len + len(part) > size and buf:
                chunks.append("".join(buf))
                buf, buf_len = [], 0
            buf.append(part)
            buf_len += len(part)
    if buf:
        chunks.append("".join(buf))
    return chunks or [""]


# ---------------------------------------------------------------- API ---------

class ZhConvertClient:
    def __init__(self, opts: ConvertOptions, session: requests.Session | None = None,
                 log: Callable[[str], None] = print):
        self.opts = opts
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "zhconvert-long/1.0"
        self.log = log
        self._last_request = 0.0
        self.max_ok: int | None = None   # 自動切小後記住的可行長度

    def _params(self, text: str) -> dict:
        o = self.opts
        p = {"text": text, "converter": o.converter}
        if o.modules:
            p["modules"] = json.dumps(o.modules)
        if o.user_pre_replace.strip():
            p["userPreReplace"] = o.user_pre_replace
        if o.user_post_replace.strip():
            p["userPostReplace"] = o.user_post_replace
        if o.user_protect_replace.strip():
            p["userProtectReplace"] = o.user_protect_replace
        if o.api_key:
            p["apiKey"] = o.api_key
        p.update(o.extra_params)
        return p

    def convert(self, text: str, should_stop: Callable[[], bool] = lambda: False) -> str:
        """轉換一段文字。若伺服器疑似因為太長而失敗，會自動對半切開重送，
        並記住可行的長度，之後較長的段落直接先切小。"""
        if not text.strip():
            return text
        can_split = len(text) >= 2 * MIN_SPLIT
        if can_split and self.max_ok and len(text) > self.max_ok:
            return self._convert_split(text, self.max_ok, should_stop)
        try:
            # 能切的段落只重試一次就改切小，免得對太長的段落白等好幾輪退避
            return self._request(text, should_stop, 1 if can_split else self.opts.retries)
        except TooLong as e:
            if not can_split:
                raise
            half = max(len(text) // 2, MIN_SPLIT)
            self.max_ok = min(self.max_ok or half, half)
            self.log(f"  ⚠ 該段 {len(text):,} 字疑似太長（{e}），自動切成約 {half:,} 字重送")
            return self._convert_split(text, half, should_stop)

    def _convert_split(self, text: str, size: int, should_stop) -> str:
        return "".join(self.convert(p, should_stop) for p in split_text(text, size))

    def _request(self, text: str, should_stop, retries: int) -> str:
        last_err = None
        for attempt in range(retries + 1):
            if should_stop():
                raise Cancelled()
            wait = self.opts.delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                self._last_request = time.monotonic()
                r = self.session.post(API_URL, data=self._params(text), timeout=self.opts.timeout)
                if r.status_code in SIZE_STATUS:
                    raise TooLong(f"HTTP {r.status_code}")
                if r.status_code == 429 or r.status_code >= 500:
                    raise ConvertError(f"HTTP {r.status_code}")
                r.raise_for_status()
                try:
                    js = r.json()
                except ValueError:
                    raise TooLong(f"伺服器回應不是 JSON（HTTP {r.status_code}）")
                if js.get("code", 0) != 0:
                    raise ConvertError(f"API 錯誤 code={js.get('code')}：{js.get('msg')}")
                return js["data"]["text"]
            except requests.Timeout as e:
                last_err = TooLong(f"逾時：{e}")
            except (requests.RequestException, ConvertError, ValueError, KeyError) as e:
                last_err = e
            if attempt < retries:
                backoff = min(2 ** attempt * 2, 60)
                self.log(f"  ⚠ 失敗（{last_err}），{backoff} 秒後重試 {attempt + 1}/{retries}")
                for _ in range(int(backoff * 10)):
                    if should_stop():
                        raise Cancelled()
                    time.sleep(0.1)
        cls = TooLong if isinstance(last_err, TooLong) else ConvertError
        raise cls(f"重試 {retries} 次仍失敗：{last_err}")


# ---------------------------------------------------------------- 續傳快取 ----

class ChunkCache:
    """把已轉好的段落存在輸出檔旁的 .zhcache，中斷後重跑會自動跳過。"""

    def __init__(self, path: str, signature: str):
        self.path = path
        self.data = {"sig": signature, "chunks": {}}
        if os.path.exists(path):
            try:
                old = json.load(open(path, encoding="utf-8"))
                if old.get("sig") == signature:
                    self.data = old
            except (OSError, ValueError):
                pass

    def get(self, i: int):
        return self.data["chunks"].get(str(i))

    def put(self, i: int, text: str):
        self.data["chunks"][str(i)] = text
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def remove(self):
        if os.path.exists(self.path):
            os.remove(self.path)


def _signature(chunks: Iterable[str], opts: ConvertOptions) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([opts.converter, opts.modules, opts.user_pre_replace,
                         opts.user_post_replace, opts.user_protect_replace,
                         opts.extra_params, opts.json_convert_keys],
                        ensure_ascii=False, sort_keys=True).encode())
    for c in chunks:
        h.update(b"\0" + c.encode("utf-8"))
    return h.hexdigest()


def convert_chunks(chunks: list[str], client: ZhConvertClient, cache: ChunkCache | None,
                   progress: Callable[[int, int], None], should_stop: Callable[[], bool]) -> list[str]:
    out = []
    for i, c in enumerate(chunks):
        cached = cache.get(i) if cache else None
        if cached is not None:
            out.append(cached)
        else:
            res = client.convert(c, should_stop)
            if cache:
                cache.put(i, res)
            out.append(res)
        progress(i + 1, len(chunks))
    return out


# ---------------------------------------------------------------- JSON --------

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


def _unesc(s: str) -> str:
    return re.sub(r"\\(\\|n|r)", lambda m: {"\\": "\\", "n": "\n", "r": "\r"}[m.group(1)], s)


def _split_for_json(s: str, size: int) -> list[str]:
    """把字串切成跳脫後每塊都不超過 size 的小塊；"".join(結果) == s。"""
    if len(_esc(s)) <= size:
        return [s]
    sub = size
    while True:
        parts = split_text(s, sub)
        if all(len(_esc(p)) <= size for p in parts) or sub <= 100:
            return parts
        sub = max(100, sub // 2)


def _collect_strings(obj, keys: bool, acc: list[str]):
    if isinstance(obj, str):
        if CJK_RE.search(obj):
            acc.append(obj)
    elif isinstance(obj, list):
        for v in obj:
            _collect_strings(v, keys, acc)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if keys and CJK_RE.search(k):
                acc.append(k)
            _collect_strings(v, keys, acc)


def _apply_strings(obj, keys: bool, mapping: dict):
    if isinstance(obj, str):
        return mapping.get(obj, obj)
    if isinstance(obj, list):
        return [_apply_strings(v, keys, mapping) for v in obj]
    if isinstance(obj, dict):
        return {(mapping.get(k, k) if keys else k): _apply_strings(v, keys, mapping)
                for k, v in obj.items()}
    return obj


def convert_json_text(text: str, client: ZhConvertClient, opts: ConvertOptions, cache_path: str | None,
                      progress, should_stop, log) -> str:
    """只轉換 JSON 裡含中文的字串值（可選含 key），結構、數字、符號完全不動。"""
    obj = json.loads(text)
    strings: list[str] = []
    _collect_strings(obj, opts.json_convert_keys, strings)
    uniq = list(dict.fromkeys(strings))
    log(f"  JSON 內含中文字串 {len(uniq)} 個（去重後）")
    if not uniq:
        progress(1, 1)
        return text

    # 超過段落上限的字串先切成小塊（跳脫後長度也要在上限內），轉完再接回
    pieces: list[str] = []          # 每個要送出的單位
    owner: list[int] = []           # 該單位屬於 uniq 的第幾個字串
    long_count = 0
    for idx, s in enumerate(uniq):
        parts = _split_for_json(s, opts.chunk_size - 1)   # 預留打包時加上的換行
        if len(parts) > 1:
            long_count += 1
        pieces.extend(parts)
        owner.extend([idx] * len(parts))
    if long_count:
        log(f"  其中 {long_count} 個字串超過每段上限，已切成小塊分別轉換")

    # 一個單位一行，打包成段；每段轉完檢查行數是否對得上
    lines = [_esc(s) + "\n" for s in pieces]
    groups, cur, cur_len = [], [], 0
    for ln in lines:
        if cur and cur_len + len(ln) > opts.chunk_size:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(ln)
        cur_len += len(ln)
    groups.append(cur)
    chunks = ["".join(g) for g in groups]

    cache = ChunkCache(cache_path, _signature(chunks, opts)) if cache_path else None
    converted = convert_chunks(chunks, client, cache, progress, should_stop)

    results: list[str] = []
    for g, src, dst in zip(groups, chunks, converted):
        out_lines = dst.split("\n")
        if out_lines and out_lines[-1] == "":
            out_lines.pop()
        if len(out_lines) != len(g):
            log("  ⚠ 某段轉換後行數不符，改為逐一轉換該段字串")
            out_lines = [_esc(client.convert(_unesc(x.rstrip("\n")), should_stop)) for x in g]
        results.extend(_unesc(x) for x in out_lines)

    joined = [""] * len(uniq)
    for idx, piece in zip(owner, results):
        joined[idx] += piece
    mapping = dict(zip(uniq, joined))
    new_obj = _apply_strings(obj, opts.json_convert_keys, mapping)
    indent = _guess_indent(text)
    out = json.dumps(new_obj, ensure_ascii=False, indent=indent)
    if cache:
        cache.remove()
    return out + ("\n" if text.endswith("\n") else "")


def _guess_indent(text: str):
    m = re.search(r"\n([ \t]+)\S", text)
    if not m:
        return None
    ws = m.group(1)
    return "\t" if ws.startswith("\t") else len(ws)


# ---------------------------------------------------------------- 主流程 ------

def output_path_for(src: str, out_dir: str | None, suffix: str) -> str:
    base, ext = os.path.splitext(os.path.basename(src))
    folder = out_dir or os.path.dirname(src)
    return os.path.join(folder, f"{base}{suffix}{ext}")


def convert_file(src: str, dst: str, opts: ConvertOptions, *, encoding: str = "auto",
                 progress: Callable[[int, int], None] = lambda a, b: None,
                 should_stop: Callable[[], bool] = lambda: False,
                 log: Callable[[str], None] = print,
                 session: requests.Session | None = None) -> None:
    text, used_enc = read_text(src, encoding)
    log(f"▶ {os.path.basename(src)}（{len(text):,} 字元，編碼 {used_enc}）")
    client = ZhConvertClient(opts, session=session, log=log)
    cache_path = dst + ".zhcache"

    is_json = src.lower().endswith(".json")
    if is_json:
        try:
            result = convert_json_text(text, client, opts, cache_path, progress, should_stop, log)
        except json.JSONDecodeError:
            log("  JSON 解析失敗，改用純文字模式")
            is_json = False
    if not is_json:
        chunks = split_text(text, opts.chunk_size)
        log(f"  切成 {len(chunks)} 段")
        cache = ChunkCache(cache_path, _signature(chunks, opts))
        done = sum(1 for i in range(len(chunks)) if cache.get(i) is not None)
        if done:
            log(f"  發現續傳快取，已完成 {done} 段，從中斷處繼續")
        result = "".join(convert_chunks(chunks, client, cache, progress, should_stop))
        cache.remove()

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with open(dst, "w", encoding=opts.output_encoding, newline="") as f:
        f.write(result)
    log(f"  ✔ 已輸出：{dst}")
