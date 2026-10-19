"""受控 HTTPS 上传客户端（契约 1.3.0，REQ-20261019-005 / C2S-6~9）。

客户机 push 通道：Bearer 租户 token 鉴权，tenant 由服务端解出；分块断点
续传（offset 严格对齐），崩溃后凭本地登记的 upload_id 续传同一会话。
内部机器继续走 rsync over ssh（transfer.py 按 upload_url 分流），本模块
只服务 HTTPS 通道。接口细节见 docs/SERVER-INTERFACE.md §4 C2S-6~9。

仅用 stdlib（urllib/hashlib）——冻结 exe 不引新依赖（exe 冻结打包坑）。
"""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from toolbox.config import DATA

# 与服务端 capabilities.upload_contract 比对的下限（主.次）
SUPPORTED_CONTRACT = (1, 3)

DEFAULT_CHUNK = 32 * 1024 * 1024        # 32MiB/块（服务端上限 64MiB）
RETRY_DELAYS = (1, 2, 4, 8, 16)         # 网络/5xx/429 退避；409/422 不走此表


class UploadError(RuntimeError):
    """服务端 4xx/5xx：code 为机器码（契约 §4 错误约定），端侧按 code 分支。
    data 保留服务端 error 对象全部字段（如 checksum_mismatch 附带 index）。"""

    def __init__(self, code, message, status=None, data=None):
        super().__init__(f"[{code}] {message}")
        self.code, self.status, self.data = code, status, (data or {})


def file_sha256(path, block=4 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(block)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class ProductionUploader:
    def __init__(self, base_url, token=None, chunk=DEFAULT_CHUNK, echo=print,
                 signer=None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.chunk = chunk
        self.echo = echo
        # 鉴权层可插拔（契约 §4A「与既有接口的关系」）：1.3.0 租户 token 用
        # token；账号模式（1.4.0 实施）经 signer 附加 DSJ-PC-1 签名头并
        # 由服务端 INT-1 核验——接口形状不变，鉴权层置换在此挂接
        self.signer = signer

    # ------------------------------------------------------------ HTTP 基座

    def _request(self, method, path, body=None, data=None, timeout=600):
        """body=dict 走 JSON；data=bytes 原样（分块 PUT）。非 2xx 一律
        UploadError（含服务端 {"error":{"code","message"}} 解析）。"""
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.signer:
            # 签「线上原样 path」：base 路径前缀 + 相对 path（与 account 同口径）
            wire = urllib.parse.urlsplit(self.base).path + path
            headers.update(self.signer(method, wire, data or b""))
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8")).get("error", {})
            except Exception:
                err = {}
            raise UploadError(err.get("code", "http_error"),
                              err.get("message", f"HTTP {e.code}"), e.code,
                              err) from None
        except urllib.error.URLError as e:
            raise UploadError("network", f"网络错误：{e.reason}") from None
        return json.loads(raw.decode("utf-8")) if raw.strip() else {}

    def _request_retry(self, method, path, body=None):
        """元数据请求（create/status/complete）：网络/5xx/429 退避重试。"""
        last = None
        for delay in (0,) + RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                return self._request(method, path, body)
            except UploadError as e:
                # 4xx（除 429）是确定性失败，重试无意义
                if e.code not in ("network", "http_error") and e.status \
                        and not (e.status >= 500 or e.status == 429):
                    raise
                last = e
        raise last

    # ------------------------------------------------------------ 接口封装

    def capabilities(self):
        return self._request("GET", "/capabilities")

    def check_capabilities(self):
        caps = self.capabilities()
        try:
            major, minor = map(int, caps["upload_contract"].split(".")[:2])
        except (KeyError, ValueError):
            raise UploadError("bad_capabilities", f"capabilities 异常：{caps}") from None
        if (major, minor) < SUPPORTED_CONTRACT or major > SUPPORTED_CONTRACT[0]:
            raise UploadError(
                "contract_mismatch",
                f"服务端上传契约 {caps['upload_contract']} 与本机支持的 "
                f"{'.'.join(map(str, SUPPORTED_CONTRACT))} 不匹配，请先升级"
                + ("服务端" if (major, minor) < SUPPORTED_CONTRACT else "客户端"))
        return caps

    def create(self, session, mode, files, run_eval=False):
        return self._request_retry("POST", "/uploads", {
            "session": session, "mode": mode, "run_eval": run_eval,
            "files": [{"path": p, "size": s, "sha256": h}
                      for p, s, h in files]})

    def status(self, upload_id):
        return self._request_retry("GET", f"/uploads/{upload_id}")

    def complete(self, upload_id, manifest, run_eval=False, no_import=False):
        return self._request_retry(
            "POST", f"/uploads/{upload_id}/complete",
            {"manifest": manifest, "run_eval": run_eval, "no_import": no_import})

    # ------------------------------------------------------------ 上传循环

    def put_chunk(self, upload_id, idx, offset, blob):
        return self._request(
            "PUT", f"/uploads/{upload_id}/files/{idx}?offset={offset}", data=blob)

    def upload_file(self, upload_id, idx, path, size, label="", start=0):
        """单文件分块续传（start=服务端已收字节）：409 实时对齐（对端可能
        已收到响应丢失的块），网络错误退避重试同一块。返回已传字节数。"""
        offset = start
        with open(path, "rb") as f:
            while offset < size:
                f.seek(offset)
                blob = f.read(self.chunk)
                last = None
                for delay in (0,) + RETRY_DELAYS:
                    if delay:
                        time.sleep(delay)
                    try:
                        r = self.put_chunk(upload_id, idx, offset, blob)
                        offset = r.get("received", offset + len(blob))
                        break
                    except UploadError as e:
                        if e.code == "offset_mismatch":
                            # 对端领先（响应丢失的块已落盘）：对齐续传
                            st = self.status(upload_id)
                            offset = st["files"][idx]["received"]
                            break
                        if e.status and e.status < 500 and e.status != 429 \
                                and e.code != "network":
                            raise
                        last = e
                else:
                    raise last
                if label and size and offset < size:
                    self.echo(f"    {label} {offset * 100 // size}%"
                              f"（{offset / 1e6:.0f}MB/{size / 1e6:.0f}MB）",
                              end="\r")
        if label and size:
            self.echo(f"    {label} 完成（{size / 1e6:.0f}MB）"
                      + " " * 12)                      # 盖掉 \r 残留
        return offset


# ---------------------------------------------------------------- 会话登记（断点续传）

def _registry_path():
    return DATA / ".uploads.json"


def files_signature(files):
    """清单签名：路径+大小+哈希 全一致才允许复用旧 upload_id。"""
    blob = "\n".join(f"{p}|{s}|{h}" for p, s, h in files)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_upload_id(session, mode, signature):
    reg = {}
    p = _registry_path()
    if p.is_file():
        try:
            reg = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            reg = {}
    ent = reg.get(f"{session}|{mode}")
    if ent and ent.get("sig") == signature and ent.get("upload_id"):
        return ent["upload_id"]
    return None


def save_upload_id(session, mode, signature, upload_id):
    p = _registry_path()
    reg = {}
    if p.is_file():
        try:
            reg = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            reg = {}
    reg[f"{session}|{mode}"] = {"sig": signature, "upload_id": upload_id,
                                "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)


def push_files(uploader, session, mode, files, local_map, manifest,
               run_eval=False, no_import=False):
    """完整 push：预检 → 复用/建会话 → 逐文件续传 → complete 拿回执。

    files: [(rel_path, size, sha256)]（仅数据/源片文件）；local_map:
    rel_path -> 本地 Path。manifest 走 complete 请求体（服务端落盘），
    不进 files —— 清单签名只含数据文件，重跑（pushed_at 变化）不破坏续传。
    complete 校验失败（422 checksum_mismatch）时服务端清零出错文件，
    此处重传该文件后重试 complete（至多 3 轮）。
    """
    uploader.check_capabilities()
    sig = files_signature(files)
    upload_id = load_upload_id(session, mode, sig)
    if upload_id:
        try:
            st = uploader.status(upload_id)
            if st.get("state") == "open":
                uploader.echo(f"续传上传会话 {upload_id}"
                              f"（{sum(f['received'] for f in st['files']) / 1e6:.0f}MB"
                              " 已在服务端）")
            else:                                  # 已 complete：直接收口
                upload_id = None
        except UploadError:
            upload_id = None                       # 过期/不存在：新建
    if not upload_id:
        r = uploader.create(session, mode, files, run_eval=run_eval)
        upload_id = r["upload_id"]
        uploader.echo(f"上传会话 {upload_id} -> 租户 {r.get('tenant')}"
                      f"（{len(files)} 个文件，"
                      f"{sum(s for _, s, _ in files) / 1e9:.2f}GB）")
        save_upload_id(session, mode, sig, upload_id)
        st = uploader.status(upload_id)
    received = [f["received"] for f in st["files"]]
    for attempt in (1, 2, 3):
        bad = -1
        for idx, (rel, size, sha) in enumerate(files):
            if received[idx] >= size:
                continue                           # 上一轮已传完（或重传轮跳过）
            received[idx] = uploader.upload_file(
                upload_id, idx, local_map[rel], size,
                label=rel.split("/")[-1], start=received[idx])
        try:
            return uploader.complete(upload_id, manifest, run_eval=run_eval,
                                     no_import=no_import)
        except UploadError as e:
            # 校验失败：服务端已清零出错文件，本轮重传后重试 complete
            if e.code != "checksum_mismatch" or attempt == 3:
                raise
            bad = e.data.get("index", -1)
            if bad < 0 or bad >= len(files):
                raise
            received[bad] = 0
            uploader.echo(f"文件校验失败，重传：{files[bad][0]}")
