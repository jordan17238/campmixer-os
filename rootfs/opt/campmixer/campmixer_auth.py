"""Signing in to the web app with a PIN.

Two programs share this file, and each is kept from knowing more than it needs.

  netconf.py, as root, reads the PIN from campmixer.txt and writes a small file from it
  (/run/campmixer/web-auth.json): a slow, salted hash of the PIN and a key for signing sign-in cookies.

  bridge.py, the unprivileged service that faces the network, reads that file. With it the bridge can check
  a PIN and recognise its own cookies. It cannot read campmixer.txt and never holds the PIN itself.

What this does and does not do, since the page is served over plain http on the local network:

  It keeps out everyone on the network who does not know the PIN, including a program that tries PINs.
  It cannot keep out someone who records the network's traffic, because the PIN and the cookie cross the
  network unencrypted. Nothing short of https can, and https on a local name means a certificate warning
  on every phone.

Python standard library only.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time

PIN_MIN, PIN_MAX = 6, 12
MADE_UP_DIGITS = 8  # typed once per phone, so the extra two digits cost little and make guessing 100 times harder
COOKIE = "campmixer"
# About a tenth of a second and 16 MB on a Pi 4 for every PIN checked, whoever is asking.
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
SCRYPT_MAXMEM = 64 * 1024 * 1024

# Well-known PINs that the pattern rules in weak_pin() do not already catch.
COMMON_PINS = frozenset(
    "123321 111222 112358 314159 271828 123654 654123 147258 258369 369258 147852 159753 951753 753951 "
    "789456 456123 321654 102030 010203 100200 123789 789123 999000 000999 124578 235689 134679 "
    "159357 357159 142536 963852 852963 741852 852456 258456 696969 420420 123450 012340 100000 200000 "
    "12344321 14725836 78945612 12312312 10203040 13572468 24681357 11112222 12121313 19191919 20202020".split()
)


def weak_pin(pin):
    """True for the PINs a stranger would try first: 000000, 123456, 654321, 135791, 121212, 123123, 112233."""
    digits = [int(ch) for ch in pin]
    if len({(b - a) % 10 for a, b in zip(digits, digits[1:])}) <= 1:
        return True  # one digit repeated, or counting up or down in even steps
    for size in range(1, len(pin) // 2 + 1):
        if len(pin) % size == 0 and pin == pin[:size] * (len(pin) // size):
            return True  # a short block repeated: 121212, 123123, 12341234
    if len(pin) % 2 == 0 and pin[::2] == pin[1::2]:
        return True  # every digit doubled: 112233
    return pin in COMMON_PINS


def tidy_pin(text):
    """A PIN as typed, without the spaces or dashes people put in to make it readable."""
    return "".join(ch for ch in text if ch not in " -\t")


def looks_like_pin(pin):
    """Whether this has the shape of a PIN at all: digits, and the right number of them."""
    return isinstance(pin, str) and pin.isascii() and pin.isdigit() and PIN_MIN <= len(pin) <= PIN_MAX


def pin_problem(pin):
    """Why this cannot be the PIN, or None if it can."""
    if not looks_like_pin(pin):
        return "must be %d to %d digits" % (PIN_MIN, PIN_MAX)
    if weak_pin(pin):
        return "is one of the first a stranger would try"
    return None


def make_pin():
    """Random digits, never one of the obvious ones."""
    while True:
        pin = "".join(secrets.choice("0123456789") for _ in range(MADE_UP_DIGITS))
        if not weak_pin(pin):
            return pin


def _mac(key, label, data=b""):
    return hmac.new(key, label + data, hashlib.sha256).digest()


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    if not text.isascii() or not all(ch.isalnum() or ch in "-_" for ch in text):
        raise ValueError("not base64")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def derive(web_key, pin):
    """What the bridge needs in order to check this PIN and sign cookies. Root's side; the result goes in a file.

    web_key is 32 random bytes that never leave root's hands and stay the same from start to start, so a phone
    that has signed in stays signed in across restarts. The cookie key is tied to the PIN's hash, so changing
    the PIN makes every cookie handed out before it worthless.
    """
    if len(web_key) != 32 or pin_problem(pin):
        raise ValueError("need a 32-byte key and a usable PIN")
    salt = _mac(web_key, b"campmixer pin salt")[:16]
    pin_hash = hashlib.scrypt(pin.encode("ascii"), salt=salt, dklen=32, maxmem=SCRYPT_MAXMEM, **SCRYPT)
    return {
        "v": 1,
        "salt": salt.hex(),
        "n": SCRYPT["n"],
        "r": SCRYPT["r"],
        "p": SCRYPT["p"],
        "hash": pin_hash.hex(),
        "session_key": _mac(web_key, b"campmixer session key", pin_hash).hex(),
    }


class Verifier:
    """The bridge's side: check a PIN, hand out sign-in cookies and recognise them again."""

    def __init__(self, data):
        if not isinstance(data, dict) or data.get("v") != 1:
            raise ValueError("not a sign-in file this bridge understands")
        self.salt = bytes.fromhex(data["salt"])
        self.hash = bytes.fromhex(data["hash"])
        self.session_key = bytes.fromhex(data["session_key"])
        self.params = {name: int(data[name]) for name in ("n", "r", "p")}
        n, r, p = self.params["n"], self.params["r"], self.params["p"]
        sane = len(self.salt) >= 8 and len(self.hash) == 32 and len(self.session_key) == 32
        sane = sane and 2**10 <= n <= 2**16 and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4
        sane = sane and 128 * n * r * p <= SCRYPT_MAXMEM // 2
        if not sane:
            raise ValueError("the sign-in file holds values that make no sense")

    @classmethod
    def load(cls, path):
        with open(path, encoding="ascii") as handle:
            return cls(json.load(handle))

    def check_pin(self, pin):
        """Slow on purpose. Call it from a worker thread."""
        if not looks_like_pin(pin):
            return False  # cannot be the PIN whatever it is; the rules are no secret
        candidate = hashlib.scrypt(pin.encode("ascii"), salt=self.salt, dklen=32, maxmem=SCRYPT_MAXMEM, **self.params)
        return hmac.compare_digest(candidate, self.hash)

    def new_token(self):
        """A sign-in cookie: a random number and this bridge's signature on it. Nothing is kept on the Pi."""
        nonce = secrets.token_bytes(16)
        return _b64(nonce) + "." + _b64(_mac(self.session_key, b"campmixer session token", nonce))

    def token_ok(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 100 or token.count(".") != 1:
            return False
        try:
            nonce, signature = (_unb64(part) for part in token.split("."))
        except ValueError:
            return False
        if len(nonce) != 16 or len(signature) != 32:
            return False
        if _b64(nonce) + "." + _b64(signature) != token:
            return False  # one way to write each cookie, not several that read the same
        return hmac.compare_digest(signature, _mac(self.session_key, b"campmixer session token", nonce))


class Throttle:
    """Makes guessing the PIN slow, whoever is guessing and from however many addresses.

    There are two limits, and a try has to get past both.

    Each address gets five wrong tries, then has to wait before each further one: 30 s, a minute, two, then
    five minutes every time. A quarter of an hour without a wrong try wipes its slate.

    An address is easy to change, so everyone together also shares a budget of 20 wrong tries, of which one
    comes back every three minutes. That is the limit that counts: over any length of time, about 500 wrong
    PINs a day at the very most. Against the 100 million eight-digit PINs that is centuries of guessing, and
    against the million six-digit ones a few years.

    While either limit says wait, a try is turned away without the PIN being looked at. A try is counted as
    wrong before it is checked and taken off again only if it turns out right, so one that is cut short
    half-way still counts.

    The price: someone who keeps hammering the page with wrong PINs can keep new phones from signing in.
    Phones that are already signed in are not affected.
    """

    FREE_TRIES = 5
    SOURCE_WAITS = (30, 60, 120, 300)
    SOURCE_MEMORY_S = 900
    BUDGET = 20
    REFILL_S = 180
    MAX_SOURCES = 512

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.sources = {}  # address -> [wrong tries, time of the last one, may try again at]
        self.budget = float(self.BUDGET)
        self.budget_time = clock()
        self.wrong_total = 0

    def _refill(self, now):
        if now > self.budget_time:
            self.budget = min(float(self.BUDGET), self.budget + (now - self.budget_time) / self.REFILL_S)
            self.budget_time = now

    def wait(self, source):
        """Seconds until this address may try a PIN. Zero means now."""
        now = self.clock()
        self._refill(now)
        entry = self.sources.get(source)
        own = entry[2] - now if entry else 0.0
        everyone = (1.0 - self.budget) * self.REFILL_S if self.budget < 1.0 else 0.0
        return max(0.0, own, everyone)

    def spend(self, source):
        """Count one try against this address and against everyone's budget. Call it before checking the PIN."""
        now = self.clock()
        self._refill(now)
        self.budget = max(0.0, self.budget - 1.0)
        self.wrong_total += 1
        entry = self.sources.get(source)
        if entry is None or now - entry[1] > self.SOURCE_MEMORY_S:
            entry = self.sources[source] = [0, now, 0.0]
        entry[0] += 1
        entry[1] = now
        if entry[0] >= self.FREE_TRIES:
            entry[2] = now + self.SOURCE_WAITS[min(entry[0] - self.FREE_TRIES, len(self.SOURCE_WAITS) - 1)]
        if len(self.sources) > self.MAX_SOURCES:
            oldest = min((name for name in self.sources if name != source), key=lambda name: self.sources[name][1])
            del self.sources[oldest]

    def refund(self, source):
        """That try was the right PIN after all: take it off again, and wipe this address's slate."""
        self.budget = min(float(self.BUDGET), self.budget + 1.0)
        self.wrong_total = max(0, self.wrong_total - 1)
        self.sources.pop(source, None)

    # The bridge keeps this in a file under /run, so that restarting the bridge (or making it fall over)
    # does not hand a guesser a fresh budget. Times are on the clock that counts from start-up.

    def dump(self):
        return {
            "v": 1,
            "budget": round(self.budget, 4),
            "budget_time": round(self.budget_time, 3),
            "wrong_total": self.wrong_total,
            "sources": {name: [entry[0], round(entry[1], 3), round(entry[2], 3)] for name, entry in self.sources.items()},
        }

    def restore(self, data):
        """Take up where an earlier bridge left off. Anything odd in the record errs on the strict side."""
        now = self.clock()
        if not isinstance(data, dict) or data.get("v") != 1:
            raise ValueError("not a record this bridge understands")
        budget, budget_time, wrong_total = float(data["budget"]), float(data["budget_time"]), int(data["wrong_total"])
        if not (0.0 <= budget <= self.BUDGET and budget_time >= 0.0 and wrong_total >= 0):
            raise ValueError("the record holds values that make no sense")
        sources = {}
        for name, entry in list(data["sources"].items())[: self.MAX_SOURCES]:
            count, last, until = int(entry[0]), float(entry[1]), float(entry[2])
            if count < 1 or not isinstance(name, str):
                raise ValueError("the record holds values that make no sense")
            last = min(last, now)
            sources[name] = [count, last, min(until, last + self.SOURCE_WAITS[-1])]
        self.budget, self.budget_time = budget, min(budget_time, now)
        self.wrong_total, self.sources = wrong_total, sources
