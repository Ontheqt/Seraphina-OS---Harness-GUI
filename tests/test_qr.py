"""T17 (docs/seraphina/YOUR_SERVER.md §2.4, §12.2): the QR encoder swfactory/qr.py, byte mode, level M, versions 1 to
10. A small standard-library reader written here, apart from the encoder (its own function-pattern map, Galois field
and block table), reads every code back: the format bits and their BCH check, the version bits from version 7, the
mask, the codewords in the standard's zigzag, the Reed-Solomon check of every block, the byte-mode segment. Penalty
scores and the mask choice are tested apart, on hand-worked matrices.

Not here: the design's comparison with reference matrices made once by a well-known encoder at development time. No
such encoder was installed on the machine this was built on, and the tests never use the network; the reader below
is the check instead, until reference matrices are recorded.
"""

from __future__ import annotations

import unittest

from swfactory import qr

# Level M: version -> (total codewords, EC codewords per block, [(blocks, data codewords)]), from ISO/IEC 18004 table 9
TABLE = {1: (26, 10, [(1, 16)]), 2: (44, 16, [(1, 28)]), 3: (70, 26, [(1, 44)]), 4: (100, 18, [(2, 32)]),
         5: (134, 24, [(2, 43)]), 6: (172, 16, [(4, 27)]), 7: (196, 18, [(4, 31)]), 8: (242, 22, [(2, 38), (2, 39)]),
         9: (292, 22, [(3, 36), (2, 37)]), 10: (346, 26, [(4, 43), (1, 44)])}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
         9: [6, 26, 46], 10: [6, 28, 50]}


def gf_tables():
    exp, log = [0] * 512, [0] * 256
    value = 1
    for i in range(255):
        exp[i], log[value] = value, i
        value = (value << 1) ^ (0x11D if value & 0x80 else 0)
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


EXP, LOG = gf_tables()


def syndromes_zero(block: list[int], ec: int) -> bool:
    """A codeword block is valid when the polynomial it spells is zero at alpha^0 .. alpha^(ec-1)."""
    for i in range(ec):
        acc = 0
        for byte in block:
            acc = (EXP[LOG[acc] + i] if acc else 0) ^ byte
        if acc:
            return False
    return True


def bch_format(data: int) -> int:
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ (0x537 if rem & 0x200 else 0)
    return ((data << 10) | (rem & 0x3FF)) ^ 0x5412


def function_map(version: int) -> list[list[bool]]:
    size = 17 + 4 * version
    f = [[False] * size for _ in range(size)]

    def area(r0, c0, h, w):
        for r in range(max(0, r0), min(size, r0 + h)):
            for c in range(max(0, c0), min(size, c0 + w)):
                f[r][c] = True
    area(0, 0, 9, 9)                       # finder, separator and format bits, top left
    area(0, size - 8, 9, 8)                # top right
    area(size - 8, 0, 8, 9)                # bottom left
    area(6, 0, 1, size)                    # timing
    area(0, 6, size, 1)
    places = ALIGN[version]
    for i, r in enumerate(places):
        for j, c in enumerate(places):
            if (i, j) in ((0, 0), (0, len(places) - 1), (len(places) - 1, 0)):
                continue
            area(r - 2, c - 2, 5, 5)
    if version >= 7:
        area(0, size - 11, 6, 3)
        area(size - 11, 0, 3, 6)
    return f


def read(rows: list[str]) -> str:
    """The text a QR matrix holds, or AssertionError naming what does not check out."""
    m = [[ch == "1" for ch in row] for row in rows]
    size = len(m)
    version = (size - 17) // 4
    assert size == 17 + 4 * version and 1 <= version <= 10, size
    bits = 0
    first = [(i, 8) for i in range(6)] + [(7, 8), (8, 8), (8, 7)] + [(8, 14 - i) for i in range(9, 15)]
    for i, (r, c) in enumerate(first):
        bits |= int(m[r][c]) << i
    second = 0
    for i in range(8):
        second |= int(m[8][size - 1 - i]) << i
    for i in range(8, 15):
        second |= int(m[size - 15 + i][8]) << i
    assert bits == second, "the two copies of the format bits differ"
    data = next((d for d in range(32) if bch_format(d) == bits), None)
    assert data is not None, f"format bits {bits:015b} are not a valid BCH code word"
    level, mask = data >> 3, data & 7
    assert level == 0, "level M"
    assert m[size - 8][8], "the dark module"
    if version >= 7:
        found = 0
        for i in range(18):
            found |= int(m[i // 3][size - 11 + i % 3]) << i
        rem = version
        for _ in range(12):
            rem = (rem << 1) ^ (0x1F25 if rem & 0x800 else 0)
        assert found == (version << 12 | (rem & 0xFFF)), "version bits"
    f = function_map(version)
    masks = [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0,
             lambda r, c: (r + c) % 3 == 0, lambda r, c: (r // 2 + c // 3) % 2 == 0,
             lambda r, c: (r * c) % 2 + (r * c) % 3 == 0, lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
             lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]
    stream = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows_order = range(size - 1, -1, -1) if upward else range(size)
        for r in rows_order:
            for c in (col, col - 1):
                if not f[r][c]:
                    stream.append(int(m[r][c] ^ masks[mask](r, c)))
        upward = not upward
        col -= 2
    total, ec, groups = TABLE[version]
    words = [int("".join(map(str, stream[i:i + 8])), 2) for i in range(0, total * 8, 8)]
    sizes = [n for count, n in groups for _ in range(count)]
    blocks = [[] for _ in sizes]
    at = 0
    for i in range(max(sizes)):
        for b, n in enumerate(sizes):
            if i < n:
                blocks[b].append(words[at])
                at += 1
    for i in range(ec):
        for b in range(len(sizes)):
            blocks[b].append(words[at])
            at += 1
    for block in blocks:
        assert syndromes_zero(block, ec), "a block fails its Reed-Solomon check"
    payload = [byte for block, n in zip(blocks, sizes) for byte in block[:n]]
    bitstring = "".join(f"{byte:08b}" for byte in payload)
    assert bitstring[:4] == "0100", "byte mode"
    width = 8 if version < 10 else 16
    count = int(bitstring[4:4 + width], 2)
    start = 4 + width
    raw = bytes(int(bitstring[start + 8 * i:start + 8 * i + 8], 2) for i in range(count))
    return raw.decode("utf-8")


ADDRESSES = [
    "https://photos.example.ts.net", "https://a.example.ts.net", "http://192.0.2.10:8081",
    "https://media.example.ts.net", "https://shared.example.ts.net:8443", "https://beszel.home.example",
    "https://notes.example.ts.net/notes", "http://127.0.0.1:9090", "https://x.ts.net",
    "https://" + "a" * 40 + ".example.ts.net", "https://" + "b" * 60 + ".example.ts.net/" + "c" * 10,
    "https://" + "d" * 80 + ".example.ts.net", "https://" + "e" * 100 + ".example.ts.net",
    "https://" + "f" * 120 + ".example.ts.net", "https://" + "g" * 140 + ".example.ts.net",
    "https://" + "h" * 160 + ".example.ts.net", "https://" + "i" * 180 + ".example.ts.net",
    "https://" + "j" * 190 + ".ts.net", "https://" + "k" * 198 + ".ts.net", "h" * 213,
    "http://a.b:1", "https://" + "m" * 32 + ".example.ts.net", "https://" + "n" * 92 + ".example.ts.net",
]


class QrTests(unittest.TestCase):
    def test_every_address_reads_back_across_versions_1_to_10(self):
        versions = set()
        for address in ADDRESSES:
            with self.subTest(address=address[:40]):
                code = qr.encode(address)
                versions.add(code["version"])
                self.assertEqual(code["size"], 17 + 4 * code["version"])
                self.assertEqual(len(code["rows"]), code["size"])
                self.assertTrue(all(len(row) == code["size"] and set(row) <= {"0", "1"} for row in code["rows"]))
                self.assertEqual(read(code["rows"]), address)
        self.assertEqual(versions, set(range(1, 11)), "the addresses cover every version 1-10")

    def test_every_forced_mask_reads_back(self):
        for mask in range(8):
            with self.subTest(mask=mask):
                code = qr.encode("https://photos.example.ts.net", mask=mask)
                self.assertEqual(code["mask"], mask)
                self.assertEqual(read(code["rows"]), "https://photos.example.ts.net")

    def test_the_mask_chosen_has_the_lowest_penalty(self):
        for address in ADDRESSES[:6]:
            scores = [qr.penalty(qr.matrix(address, mask).modules) for mask in range(8)]
            chosen = qr.encode(address)["mask"]
            self.assertEqual(scores[chosen], min(scores))
            self.assertEqual(chosen, scores.index(min(scores)), "the first of equal scores")

    def test_penalty_rules_on_hand_worked_matrices(self):
        dark = [[True] * 5 for _ in range(5)]
        # N1: 10 runs of 5 (3 each) = 30; N2: 16 blocks (3 each) = 48; N3: none; N4: 100% dark, 10 steps = 100
        self.assertEqual(qr.penalty(dark), 30 + 48 + 100)
        board = [[(r + c) % 2 == 0 for c in range(6)] for r in range(6)]
        self.assertEqual(qr.penalty(board), 0)
        # one finder-like row (1 0 1 1 1 0 1 then four light) at the top of an otherwise light 11 x 11 matrix:
        # N1: row 0's light run of 4 counts nothing; rows 1-10 are 11-runs (3 + 6 each) = 90; the 5 columns with a
        # dark top have a light run of 10 (3 + 5 each) = 40, the other 6 columns are 11-runs (3 + 6 each) = 54;
        # N2: rows 1-10 make 9 x 10 light blocks (3 each) = 270, and rows 0-1 three more at columns 7-10 = 9;
        # N3: one pattern = 40; N4: 5 dark of 121 is 4.1%, 9 steps = 90
        light = [[False] * 11 for _ in range(11)]
        light[0] = [True, False, True, True, True, False, True, False, False, False, False]
        self.assertEqual(qr.penalty(light), 90 + 40 + 54 + 270 + 9 + 40 + 90)

    def test_too_long_and_empty_are_refused(self):
        with self.assertRaises(ValueError):
            qr.encode("x" * 214)
        with self.assertRaises(ValueError):
            qr.encode("")

    def test_the_terminal_drawing_has_two_rows_per_line(self):
        code = qr.encode("https://photos.example.ts.net")
        lines = qr.terminal(code["rows"]).split("\n")
        self.assertEqual(len(lines[0]), code["size"] + 4)
        self.assertEqual(len(lines), (code["size"] + 5) // 2)


if __name__ == "__main__":
    unittest.main()
