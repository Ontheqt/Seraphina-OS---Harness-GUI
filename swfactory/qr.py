"""A small QR code encoder for the Your server area's app addresses (docs/seraphina/YOUR_SERVER.md §2.4; DESIGN.md
section 42): byte mode, error correction level M, versions 1 to 10 (an address of up to 213 bytes), the eight masks
scored by the standard's penalty rules (ISO/IEC 18004, section 7.8.3) and the lowest one kept.

encode(text) gives {version, mask, size, rows}: rows is one string of 0s and 1s per row (1 is dark), without the
quiet border, which the view draws (four squares). The cockpit makes a code only from an address the owner
confirmed; it never leaves the screen (no notice, item, event or model's context).

Standard library only, no state.
"""

from __future__ import annotations

from typing import Iterable

# Level M, versions 1-10: (total codewords, EC codewords per block, [(blocks, data codewords per block), ...])
BLOCKS = {
    1: (26, 10, [(1, 16)]), 2: (44, 16, [(1, 28)]), 3: (70, 26, [(1, 44)]), 4: (100, 18, [(2, 32)]),
    5: (134, 24, [(2, 43)]), 6: (172, 16, [(4, 27)]), 7: (196, 18, [(4, 31)]), 8: (242, 22, [(2, 38), (2, 39)]),
    9: (292, 22, [(3, 36), (2, 37)]), 10: (346, 26, [(4, 43), (1, 44)]),
}
ALIGNMENT = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
             9: [6, 26, 46], 10: [6, 28, 50]}
LEVEL_M = 0b00               # the two format bits of level M (L 01, M 00, Q 11, H 10)
MAX_VERSION = 10
MAX_BYTES = 213              # byte mode at 10-M
N1, N2, N3, N4 = 3, 3, 40, 10


def data_codewords(version: int) -> int:
    return sum(count * size for count, size in BLOCKS[version][2])


def capacity(version: int) -> int:
    """The most bytes byte mode holds at version-M (4 mode bits, 8 or 16 count bits)."""
    count_bits = 8 if version < 10 else 16
    return (data_codewords(version) * 8 - 4 - count_bits) // 8


def pick_version(length: int) -> int:
    for version in range(1, MAX_VERSION + 1):
        if length <= capacity(version):
            return version
    raise ValueError(f"an address of {length} bytes is too long for a QR code here (at most {MAX_BYTES})")


# ---------------------------------------------------------------- Reed-Solomon over GF(256), polynomial 0x11D

EXP = [0] * 512
LOG = [0] * 256
_value = 1
for _i in range(255):
    EXP[_i] = _value
    LOG[_value] = _i
    _value <<= 1
    if _value & 0x100:
        _value ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def rs_generator(degree: int) -> list[int]:
    """The generator polynomial's coefficients, highest power first, the leading 1 left out."""
    poly = [1]
    for i in range(degree):
        poly = [a ^ gf_mul(b, EXP[i]) for a, b in zip(poly + [0], [0] + poly)]
    return poly[1:]


def rs_remainder(data: Iterable[int], degree: int) -> list[int]:
    gen = rs_generator(degree)
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i, coef in enumerate(gen):
            rem[i] ^= gf_mul(coef, factor)
    return rem


# ---------------------------------------------------------------- the bits


def codewords(text: str, version: int) -> list[int]:
    """The data codewords (mode, count, bytes, terminator, padding), then each block's EC codewords, interleaved."""
    raw = text.encode("utf-8")
    bits: list[int] = []

    def put(value: int, width: int) -> None:
        bits.extend((value >> shift) & 1 for shift in range(width - 1, -1, -1))

    put(0b0100, 4)
    put(len(raw), 8 if version < 10 else 16)
    for byte in raw:
        put(byte, 8)
    room = data_codewords(version) * 8
    put(0, min(4, room - len(bits)))
    if len(bits) % 8:
        put(0, 8 - len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < data_codewords(version):
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    _, ec, groups = BLOCKS[version]
    blocks, at = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(data[at:at + size])
            at += size
    out = [block[i] for i in range(max(len(b) for b in blocks)) for block in blocks if i < len(block)]
    eccs = [rs_remainder(block, ec) for block in blocks]
    out += [ecc[i] for i in range(ec) for ecc in eccs]
    return out


def format_bits(mask: int, level: int = LEVEL_M) -> int:
    data = level << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data << 10 | rem) ^ 0x5412


def version_bits(version: int) -> int:
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return version << 12 | rem


def mask_bit(mask: int, row: int, col: int) -> bool:
    return [
        (row + col) % 2 == 0, row % 2 == 0, col % 3 == 0, (row + col) % 3 == 0, (row // 2 + col // 3) % 2 == 0,
        (row * col) % 2 + (row * col) % 3 == 0, ((row * col) % 2 + (row * col) % 3) % 2 == 0,
        ((row + col) % 2 + (row * col) % 3) % 2 == 0,
    ][mask]


# ---------------------------------------------------------------- the matrix


class Matrix:
    """modules[row][col] (True dark) and which modules are function patterns."""

    def __init__(self, version: int) -> None:
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self._draw_functions()

    def set(self, row: int, col: int, dark: bool) -> None:
        self.modules[row][col] = dark
        self.function[row][col] = True

    def _draw_functions(self) -> None:
        size = self.size
        for i in range(size):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for row, col in ((3, 3), (3, size - 4), (size - 4, 3)):
            for dr in range(-4, 5):
                for dc in range(-4, 5):
                    r, c = row + dr, col + dc
                    if 0 <= r < size and 0 <= c < size:
                        self.set(r, c, max(abs(dr), abs(dc)) not in (2, 4))
        places = ALIGNMENT[self.version]
        last = len(places) - 1
        for i, row in enumerate(places):
            for j, col in enumerate(places):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        self.set(row + dr, col + dc, max(abs(dr), abs(dc)) != 1)
        self.draw_format(0)
        if self.version >= 7:
            bits = version_bits(self.version)
            for i in range(18):
                dark = bool((bits >> i) & 1)
                a, b = size - 11 + i % 3, i // 3
                self.set(b, a, dark)
                self.set(a, b, dark)

    def draw_format(self, mask: int) -> None:
        bits = format_bits(mask)
        size = self.size

        def bit(i: int) -> bool:
            return bool((bits >> i) & 1)

        for i in range(6):
            self.set(i, 8, bit(i))
        self.set(7, 8, bit(6))
        self.set(8, 8, bit(7))
        self.set(8, 7, bit(8))
        for i in range(9, 15):
            self.set(8, 14 - i, bit(i))
        for i in range(8):
            self.set(8, size - 1 - i, bit(i))
        for i in range(8, 15):
            self.set(size - 15 + i, 8, bit(i))
        self.set(size - 8, 8, True)          # the dark module

    def place(self, data: list[int]) -> None:
        """The codewords in the standard's zigzag, two columns at a time from the right, skipping the timing column."""
        size, i, total = self.size, 0, len(data) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            upward = ((right + 1) & 2) == 0
            for vert in range(size):
                row = size - 1 - vert if upward else vert
                for j in range(2):
                    col = right - j
                    if not self.function[row][col] and i < total:
                        self.modules[row][col] = bool((data[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for row in range(self.size):
            for col in range(self.size):
                if not self.function[row][col] and mask_bit(mask, row, col):
                    self.modules[row][col] = not self.modules[row][col]

    def rows(self) -> list[str]:
        return ["".join("1" if dark else "0" for dark in line) for line in self.modules]


def penalty(modules: list[list[bool]]) -> int:
    """The standard's four penalty rules over a finished matrix: runs of five or more of one colour in a row or
    column (N1 + one per module beyond five), 2x2 blocks of one colour (N2 each), the finder-like 1:1:3:1:1 pattern
    with four light modules on one side (N3 each, rows and columns, inside the matrix), and the dark share's
    distance from 50% in steps of 5% (N4 each)."""
    size = len(modules)
    score = 0
    lines = [list(row) for row in modules] + [[modules[r][c] for r in range(size)] for c in range(size)]
    for line in lines:
        run, last = 0, None
        for dark in line:
            if dark == last:
                run += 1
            else:
                if run >= 5:
                    score += N1 + run - 5
                run, last = 1, dark
        if run >= 5:
            score += N1 + run - 5
    for r in range(size - 1):
        for c in range(size - 1):
            if modules[r][c] == modules[r][c + 1] == modules[r + 1][c] == modules[r + 1][c + 1]:
                score += N2
    patterns = ([True, False, True, True, True, False, True, False, False, False, False],
                [False, False, False, False, True, False, True, True, True, False, True])
    for line in lines:
        for start in range(size - 10):
            window = line[start:start + 11]
            if window == patterns[0] or window == patterns[1]:
                score += N3
    dark = sum(sum(1 for m in row if m) for row in modules)
    percent = dark * 100 / (size * size)
    score += int(abs(percent - 50) // 5) * N4
    return score


def matrix(text: str, mask: int | None = None) -> Matrix:
    """The finished code for text, with mask forced (0-7) or the lowest-scoring one."""
    raw = text.encode("utf-8")
    version = pick_version(len(raw))
    data = codewords(text, version)
    best: Matrix | None = None
    best_score = None
    for candidate in ([mask] if mask is not None else range(8)):
        if not 0 <= candidate <= 7:
            raise ValueError("mask must be 0 to 7")
        m = Matrix(version)
        m.place(data)
        m.apply_mask(candidate)
        m.draw_format(candidate)
        m.mask = candidate          # type: ignore[attr-defined]
        score = penalty(m.modules)
        if best_score is None or score < best_score:
            best, best_score = m, score
    assert best is not None
    return best


def encode(text: str, mask: int | None = None) -> dict:
    """{version, mask, size, rows} for text (ValueError when it is empty or longer than 213 bytes)."""
    if not isinstance(text, str) or not text:
        raise ValueError("there is nothing to encode")
    m = matrix(text, mask)
    return {"version": m.version, "mask": m.mask, "size": m.size, "rows": m.rows()}   # type: ignore[attr-defined]


def terminal(rows: list[str], quiet: int = 2) -> str:
    """The code drawn with block characters for a terminal (`factory apps qr`): two rows per line."""
    width = len(rows[0]) if rows else 0
    padded = ["0" * (width + 2 * quiet)] * quiet + ["0" * quiet + row + "0" * quiet for row in rows] + \
        ["0" * (width + 2 * quiet)] * (quiet + 1)
    out = []
    for top, bottom in zip(padded[0::2], padded[1::2]):
        line = []
        for a, b in zip(top, bottom):
            line.append({("1", "1"): " ", ("1", "0"): "▄", ("0", "1"): "▀", ("0", "0"): "█"}[(a, b)])
        out.append("".join(line))
    return "\n".join(out)
