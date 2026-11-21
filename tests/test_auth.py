#!/usr/bin/env python3
"""Checks the PIN sign-in's building blocks: which PINs are allowed, the hash, the cookies and the guess limit."""
import json
import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "rootfs", "opt", "campmixer"))
import campmixer_auth as auth  # noqa: E402

# --- which PINs are allowed
for weak in ("000000", "111111", "999999", "123456", "234567", "654321", "987654", "890123", "135791", "024680", "975319",
             "121212", "123123", "112233", "121212", "696969", "159753", "147258", "789456", "123321", "314159",
             "12345678", "87654321", "00000000", "12341234", "11223344", "12121212", "123123123", "123456789012",
             "1234", "4321", "0000", "1111", "9999", "1212", "6969", "1122", "2468", "1357", "9876", "0123", "2580", "0852", "1004",
             "1987", "1999", "2000", "2024", "2026", "1900", "2099", "12345", "54321", "11111", "13579"):
    assert auth.weak_pin(weak), weak
    assert auth.pin_problem(weak) == "is one of the first a stranger would try", weak
    assert auth.pin_problem(weak, True) is None, weak  # unless the owner has said to keep an easy one
    assert auth.guess_limit(weak) == auth.SHORT_LIMIT, weak  # which then gets the strict allowance, whatever its length
for fine in ("482719", "305918", "48271935", "90517362", "663104", "700214", "281746593021", "4827", "9051", "7305", "0394", "3187",
             "63104", "40718"):
    assert not auth.weak_pin(fine) and auth.pin_problem(fine) is None, fine
for not_a_pin in ("", "1", "123", "482", "1234567890123", "48271x", "482 719", "-482719", "+482719", "4827.19", "４８２７１９３５",
                  "٤٨٢٧١٩٣٥", "48271935\n", "４８２７", None, 48271935, 4827, b"48271935", ["48271935"]):
    assert auth.pin_problem(not_a_pin) == "must be 4 to 12 digits", repr(not_a_pin)
    assert auth.pin_problem(not_a_pin, True) == "must be 4 to 12 digits", repr(not_a_pin)  # the owner's say-so does not stretch to this
assert auth.tidy_pin(" 4827 1935 ") == "48271935" and auth.tidy_pin("4827-1935") == "48271935"
made = {auth.make_pin() for _ in range(2000)}
assert len(made) > 1990 and all(len(pin) == 8 and auth.pin_problem(pin) is None for pin in made)
# The rules must not eat the space they protect: of a million six-digit PINs, nearly all stay usable.
refused = sum(auth.weak_pin("%06d" % n) for n in range(10**6))
assert 1000 < refused < 3000, refused
refused = sum(auth.weak_pin("%04d" % n) for n in range(10**4))
assert 400 < refused < 600, refused  # of ten thousand four-digit PINs, about one in twenty is turned down

# --- the hash and the cookies
key_a, key_b = secrets.token_bytes(32), secrets.token_bytes(32)
record = auth.derive(key_a, "48271935")
assert set(record) == {"v", "budget", "refill_s", "salt", "n", "r", "p", "hash", "session_key"}
# The allowance of wrong tries comes with the PIN: about 500 a day for six digits and up, about 100 for four or five.
assert (record["budget"], record["refill_s"]) == (20, 180) and auth.derive(key_a, "482719")["budget"] == 20
short = auth.derive(key_a, "4827")
assert (short["budget"], short["refill_s"]) == (10, 900) and auth.derive(key_a, "63104")["budget"] == 10
assert auth.Verifier(short).limit == (10, 900) and auth.Verifier(record).limit == (20, 180)
assert auth.Verifier(short).check_pin("4827") and not auth.Verifier(short).check_pin("4828")
assert auth.Verifier({k: v for k, v in record.items() if k not in ("budget", "refill_s")}).limit == (20, 180)
assert "48271935" not in json.dumps(record) and key_a.hex() not in json.dumps(record)
assert auth.derive(key_a, "48271935") == record  # the same from one start-up to the next
check = auth.Verifier(json.loads(json.dumps(record)))
assert check.check_pin("48271935")
for wrong in ("48271936", "4827193", "482719350", "", "0", "x" * 8, "48271935 ", " 48271935", "４８２７１９３５", None, 48271935, b"48271935",
              ["48271935"], {"pin": "48271935"}, "48271935\x00", "9" * 5000):
    assert check.check_pin(wrong) is False, repr(wrong)
for bad_key, bad_pin in ((b"short", "48271935"), (key_a, "123456"), (key_a, "12"), (key_a, "123"), (key_a, "1234"), (key_a, "1948"),
                         (key_a + b"x", "48271935")):
    try:
        auth.derive(bad_key, bad_pin)
        raise AssertionError("derive accepted %r %r" % (bad_key, bad_pin))
    except ValueError:
        pass
# An easy PIN is put to use only on the owner's say-so, and then with the strict allowance even at six digits or more.
for easy_pin in ("1948", "1234", "123456", "12341234"):
    easy = auth.derive(key_a, easy_pin, True)
    assert (easy["budget"], easy["refill_s"]) == (10, 900), easy_pin
    assert auth.Verifier(easy).check_pin(easy_pin) and not auth.Verifier(easy).check_pin("48271935"), easy_pin
assert auth.derive(key_a, "48271935", True) == record  # the say-so changes nothing for a PIN that is not easy
for bad_key, bad_pin in ((b"short", "1948"), (key_a, "12"), (key_a, "123"), (key_a, "1234567890123"), (key_a, "19x8")):
    try:
        auth.derive(bad_key, bad_pin, True)
        raise AssertionError("derive accepted %r %r" % (bad_key, bad_pin))
    except ValueError:
        pass

token = check.new_token()
assert check.token_ok(token) and len(token) == 66 and check.new_token() != token
other_mixer = auth.Verifier(auth.derive(key_b, "48271935"))  # the same PIN on another mixer
other_pin = auth.Verifier(auth.derive(key_a, "90517362"))  # this mixer after its PIN was changed
assert not other_mixer.token_ok(token) and not other_pin.token_ok(token)
assert other_mixer.hash != check.hash and other_mixer.salt != check.salt and other_pin.salt == check.salt
nonce, signature = token.split(".")
flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
# The same bytes written another way (the last character of each part has bits to spare) are not the same cookie.
spare = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
respelt_nonce = nonce[:-1] + spare[spare.index(nonce[-1]) ^ 1]
respelt_signature = signature[:-1] + spare[spare.index(signature[-1]) ^ 1]
for forged in ("", ".", "..", token + "x", token[:-1], token[1:], nonce + "." + flipped, signature + "." + nonce, nonce + ".",
               "." + signature, nonce + "." + signature + "." + signature, token.replace(".", ""), token + "=",
               token.replace("-", "+").replace("_", "/") if ("-" in token or "_" in token) else "",
               respelt_nonce + "." + signature, nonce + "." + respelt_signature,
               "A" * 22 + "." + "A" * 43, "é" * 30 + "." + "é" * 30, "x" * 5000, " " + token, token + " ", token + "\n",
               nonce + "." + other_mixer.new_token().split(".")[1], None, 5, b"x", [token], nonce[:21] + "." + signature):
    assert check.token_ok(forged) is False, repr(forged)
# Guessing a signature is hopeless, and nothing short of the whole of it will do.
for _ in range(2000):
    assert not check.token_ok(nonce + "." + auth._b64(secrets.token_bytes(32)))
for cut in range(1, 43):
    assert not check.token_ok(nonce + "." + signature[:cut])

for nonsense in (None, [], "text", {}, {"v": 2}, dict(record, v=2), dict(record, salt="zz"), dict(record, salt="00"), dict(record, hash="00"),
                 dict(record, session_key="00" * 31), dict(record, n=3), dict(record, n=2**20), dict(record, n=0), dict(record, r=0),
                 dict(record, p=99), dict(record, n="many"), {k: v for k, v in record.items() if k != "hash"},
                 dict(record, budget=0), dict(record, budget=5000), dict(record, refill_s=1), dict(record, budget="lots")):
    try:
        auth.Verifier(nonsense)
        raise AssertionError("accepted %r" % (nonsense,))
    except (ValueError, KeyError, TypeError):
        pass


# --- the guess limit, on a clock the test moves by hand
class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


clock = Clock()
limit = auth.Throttle(clock)
assert limit.wait("phone") == 0
for _ in range(4):
    limit.spend("phone")
    assert limit.wait("phone") == 0  # four wrong tries cost nothing
waits = []
for _ in range(6):
    limit.spend("phone")
    waits.append(limit.wait("phone"))
    assert limit.wait("phone") > 0 and limit.wait("laptop") == 0  # only the one doing the guessing waits
    clock.now += limit.wait("phone") - 0.5
    assert 0 < limit.wait("phone") <= 0.5
    clock.now += 0.5
    assert limit.wait("phone") == 0
assert waits == [30, 60, 120, 300, 300, 300], waits
assert limit.wrong_total == 10

# The right PIN wipes that address's slate and gives its try back.
budget = limit.budget
limit.spend("phone")
limit.refund("phone")
assert limit.wait("phone") == 0 and limit.wrong_total == 10 and abs(limit.budget - budget) < 1e-9
for _ in range(4):
    limit.spend("phone")
assert limit.wait("phone") == 0

# A quarter of an hour without a wrong try wipes it too.
clock.now += 10 * 3600
limit = auth.Throttle(clock)
for _ in range(5):
    limit.spend("phone")
assert limit.wait("phone") == 30
clock.now += 901
assert limit.wait("phone") == 0
for _ in range(4):
    limit.spend("phone")
    assert limit.wait("phone") == 0
limit.spend("phone")
assert limit.wait("phone") == 30

# Changing address does not help: everyone together gets 20 wrong tries, then one every three minutes.
clock.now += 10 * 3600
limit = auth.Throttle(clock)
for n in range(20):
    assert limit.wait("address-%d" % n) == 0
    limit.spend("address-%d" % n)
assert 179 < limit.wait("address-new") <= 180 and 179 < limit.wait("address-0") <= 180
clock.now += 179
assert limit.wait("address-new") > 0
clock.now += 1
assert limit.wait("address-new") == 0
limit.spend("address-new")
assert limit.wait("yet-another") > 179

# The most anyone can get through in a day, trying the moment a try is allowed, from a fresh address each time.
clock.now += 10 * 3600
limit = auth.Throttle(clock)
start, tries = clock.now, 0
while clock.now - start < 86400:
    name = "bot-%d" % tries
    wait = limit.wait(name)
    if wait > 0:
        clock.now += wait
        continue
    limit.spend(name)
    tries += 1
assert 480 <= tries <= 501, tries
assert len(limit.sources) <= limit.MAX_SOURCES
# The same from a single address is slower still.
clock.now += 10 * 3600
limit = auth.Throttle(clock)
start, tries = clock.now, 0
while clock.now - start < 86400:
    wait = limit.wait("one")
    if wait > 0:
        clock.now += wait
        continue
    limit.spend("one")
    tries += 1
assert tries < 300, tries

# --- a short PIN's allowance: ten wrong tries for everyone together, then one every quarter of an hour
clock.now += 10 * 3600
limit = auth.Throttle(clock, auth.SHORT_LIMIT)
for n in range(10):
    assert limit.wait("address-%d" % n) == 0
    limit.spend("address-%d" % n)
assert 899 < limit.wait("address-new") <= 900
clock.now += 900
assert limit.wait("address-new") == 0
limit.spend("address-new")
assert limit.wait("yet-another") > 899
clock.now += 10 * 3600
limit = auth.Throttle(clock, auth.SHORT_LIMIT)
start, tries = clock.now, 0
while clock.now - start < 86400:
    name = "bot-%d" % tries
    wait = limit.wait(name)
    if wait > 0:
        clock.now += wait
        continue
    limit.spend(name)
    tries += 1
assert 96 <= tries <= 107, tries  # against some 9,500 usable four-digit PINs: about a one-in-a-hundred chance a day
# Changing the PIN to a short one tightens the allowance at once, and what was in hand does not carry over.
clock.now += 10 * 3600
limit = auth.Throttle(clock)
for n in range(3):
    limit.spend("a-%d" % n)
limit.set_limit(auth.SHORT_LIMIT)
assert limit.budget == 10 and limit.wrong_total == 3
for n in range(10):
    limit.spend("b-%d" % n)
assert limit.wait("c") > 899
limit.set_limit(auth.LIMIT)  # and back to a long one: nothing is handed out, it only comes back faster
assert 179 < limit.wait("c") <= 180

# --- the record that outlives a restart of the bridge
clock.now += 10 * 3600
limit = auth.Throttle(clock)
for _ in range(7):
    limit.spend("phone")
for n in range(9):
    limit.spend("other-%d" % n)
saved = json.loads(json.dumps(limit.dump()))
clock.now += 5
again = auth.Throttle(clock)
again.restore(saved)
assert again.wrong_total == 16 and abs(again.wait("phone") - limit.wait("phone")) < 0.01 and again.wait("phone") > 100
assert abs(again.budget - limit.budget) < 0.01 and again.wait("other-0") == 0
for _ in range(4):
    again.spend("fresh")
assert again.wait("fresh") > 0  # the shared budget was carried over as well: 16 + 4 used

assert saved["limit"] == [20, 180]
strict = auth.Throttle(clock, auth.SHORT_LIMIT)  # a record written under the looser allowance, read under the strict one
strict.restore(dict(saved, budget=18.5))
assert strict.budget == 10 and strict.wrong_total == 16
for nonsense in (None, [], {}, {"v": 2}, dict(saved, budget="lots"), dict(saved, budget=-1), dict(saved, budget=999), dict(saved, wrong_total=-5),
                 dict(saved, sources=[]), dict(saved, sources={"x": [0, 1, 2]}), dict(saved, sources={"x": "y"}), dict(saved, budget_time=-1)):
    fresh = auth.Throttle(clock)
    try:
        fresh.restore(nonsense)
        raise AssertionError("restored %r" % (nonsense,))
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        pass
    assert fresh.budget == fresh.BUDGET and fresh.wrong_total == 0  # a refused record changes nothing
# A record from the future (it cannot happen on one start-up's clock) is not allowed to lock anyone out for long.
future = dict(saved, budget_time=clock.now + 1e6, sources={"phone": [9, clock.now + 1e6, clock.now + 2e6]})
late = auth.Throttle(clock)
late.restore(future)
assert late.wait("phone") <= 300

print("sign-in: all checks passed")
