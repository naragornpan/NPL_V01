"""HTTP layer — จัดการ encoding, rate limit, retry, robots.txt

หัวใจของไฟล์นี้คือ decode(): เว็บราชการไทยรุ่นเก่าส่ง TIS-620 / windows-874
ถ้าปล่อยให้ requests เดา encoding เอง ภาษาไทยจะกลายเป็นขยะ
และจะไม่รู้ตัวจนกว่าจะเก็บข้อมูลไปแล้วเป็นเดือน
"""
from __future__ import annotations

import hashlib
import logging
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

USER_AGENT = (
    "npa-ingest/0.1 (personal research project; "
    "contact: REPLACE_WITH_YOUR_EMAIL)"
)

# encoding ที่เว็บไทยรุ่นเก่าใช้ เรียงตามลำดับที่จะลอง
THAI_FALLBACK_ENCODINGS = ("utf-8", "tis-620", "cp874", "iso-8859-11")


@dataclass
class Response:
    url: str
    status: int
    text: str
    content_hash: str


class Fetcher:
    def __init__(self, encoding: str = "utf-8", rate_limit_s: float = 3.0,
                 timeout: int = 25, respect_robots: bool = True):
        self.encoding = encoding
        self.rate_limit_s = rate_limit_s
        self.timeout = timeout
        self.respect_robots = respect_robots
        self._last_request_at = 0.0
        self._robots_cache: dict[str, robotparser.RobotFileParser] = {}
        self.bytes_downloaded = 0        # นับดาต้าที่ใช้ สำคัญตอนรันผ่านมือถือ

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        # เว็บราชการบางหน้าตอบช้า/หลุดเป็นครั้งคราว — ตั้งให้ "พลาดเร็ว"
        # แทนที่จะรอนาน เพราะทั้งงานจะได้ไม่ค้างเพราะทรัพย์ตัวเดียว
        # (เดิม total=3 backoff=2 + timeout 30 = ~130 วิ/ตัวที่ timeout)
        retry = Retry(
            total=1,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.rate_limit_s:
            time.sleep(self.rate_limit_s - elapsed)
        self._last_request_at = time.monotonic()

    def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        if root not in self._robots_cache:
            rp = robotparser.RobotFileParser()
            rp.set_url(urljoin(root, "/robots.txt"))
            try:
                rp.read()
            except Exception as exc:                       # noqa: BLE001
                log.warning("อ่าน robots.txt ไม่ได้ (%s) — ถือว่าอนุญาต", exc)
                rp = None
            self._robots_cache[root] = rp
        rp = self._robots_cache[root]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def decode(self, raw: bytes) -> str:
        """decode ให้ถูก encoding

        กฎข้อแรกสำคัญที่สุด: ถ้า decode เป็น UTF-8 แบบเข้มงวดผ่าน ให้ใช้ UTF-8
        เพราะไบต์ TIS-620 ของภาษาไทยแทบไม่มีทางประกอบเป็น UTF-8 ที่ถูกต้องได้

        กับดักที่เคยพลาดมาแล้ว: ถ้าเอา UTF-8 ไป decode ด้วย cp874 จะได้
        mojibake ที่เป็นอักษรไทยล้วน (เธ, เน, ...) ทำให้การนับสัดส่วนอักษรไทย
        บอกว่า "ถูกแล้ว" ทั้งที่ผิด — จึงห้ามใช้สัดส่วนไทยตัดสินเพียงอย่างเดียว
        """
        try:
            text = raw.decode("utf-8")
            if self.encoding.lower().replace("_", "-") not in ("utf-8", "utf8"):
                log.warning(
                    "หน้านี้เป็น UTF-8 แต่ตั้งค่าไว้เป็น %s — ใช้ UTF-8 แทน "
                    "(แก้ค่า encoding ในตาราง sources ด้วย)", self.encoding)
            return text
        except UnicodeDecodeError:
            pass

        candidates = (self.encoding,) + THAI_FALLBACK_ENCODINGS
        best, best_score = None, -1.0
        for enc in candidates:
            try:
                text = raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
            if self._looks_like_mojibake(text):
                continue
            score = self._thai_ratio(text)
            if enc == self.encoding and score > 0.01:
                return text
            if score > best_score:
                best, best_score = text, score
        if best is None:
            best = raw.decode(self.encoding, errors="replace")
        return best

    @staticmethod
    def _looks_like_mojibake(text: str) -> bool:
        """จับ mojibake แบบ UTF-8-อ่านเป็น-cp874

        ลายเซ็นคือ 'เธ' หรือ 'เน' โผล่ถี่ผิดธรรมชาติ
        ภาษาไทยจริงไม่มีทางมีสองพยางค์นี้ถี่ขนาดนั้น
        """
        sample = text[:20000]
        if len(sample) < 200:
            return False
        hits = sample.count("เธ") + sample.count("เน") + sample.count("à¸")
        return hits / len(sample) > 0.02

    @staticmethod
    def _thai_ratio(text: str) -> float:
        if not text:
            return 0.0
        sample = text[:20000]
        thai = sum(1 for ch in sample if "\u0e00" <= ch <= "\u0e7f")
        return thai / len(sample)

    # ------------------------------------------------------------------
    def get(self, url: str, **kwargs) -> Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, data: dict | None = None, **kwargs) -> Response:
        return self._request("POST", url, data=data, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> Response:
        if not self._robots_allows(url):
            raise PermissionError(f"robots.txt ไม่อนุญาตให้ดึง: {url}")
        self._throttle()
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        # ขนาดที่ผ่านเน็ตจริงหลังบีบอัด ไม่ใช่ขนาดหลังคลาย
        self.bytes_downloaded += int(
            resp.headers.get("Content-Length") or len(resp.content))
        text = self.decode(resp.content)
        return Response(
            url=url,
            status=resp.status_code,
            text=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
