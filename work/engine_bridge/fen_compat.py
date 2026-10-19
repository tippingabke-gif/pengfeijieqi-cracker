"""Translate the GUI's ordinary JieQi FEN into the ACE UCI_PF form.

The GUI hides unrevealed pieces as ``X``/``x`` and places the remaining-piece
inventory after the side-to-move field.  ACE keeps the unrevealed movement type
(``D``..``J``/``d``..``j``) on the board and requires the inventory to be
prefixed by ``$`` before the side field.  Only the well-defined starting dark
piece squares are translated; guessing an X on another square would change the
position and is rejected.
"""
from __future__ import annotations

import re


class FenCompatibilityError(ValueError):
    """The GUI FEN is not a lossless JieQi position we can translate."""


# Coordinates use file 0..8 and rank 0..9.  The first FEN row is rank 9.
_RED_DARK = {
    (0, 0): "D", (8, 0): "D",
    (1, 0): "E", (7, 0): "E",
    (2, 0): "F", (6, 0): "F",
    (3, 0): "G", (5, 0): "G",
    (1, 2): "I", (7, 2): "I",
    (0, 3): "J", (2, 3): "J", (4, 3): "J", (6, 3): "J", (8, 3): "J",
}
_BLACK_DARK = {(file, 9 - rank): value.lower()
               for (file, rank), value in _RED_DARK.items()}
_DARK = {**_RED_DARK, **_BLACK_DARK}
_INVENTORY_PAIR = re.compile(r"([A-Za-z])([0-9])")
_INVENTORY_PIECES = set("KABNRCPkabnrcp")
_LEGAL_BOARD_PIECES = set("KABNRCPkabnrcpXxDEFGIJdefgij")


def _board_to_pf(board: str) -> str:
    rows = board.split("/")
    if len(rows) != 10:
        raise FenCompatibilityError("JieQi board must contain 10 ranks")
    converted: list[str] = []
    for row_index, row in enumerate(rows):
        file = 0
        output: list[str] = []
        for char in row:
            if "1" <= char <= "9":
                file += ord(char) - ord("0")
                output.append(char)
                continue
            if char in _LEGAL_BOARD_PIECES:
                if char in "Xx":
                    rank = 9 - row_index
                    try:
                        expected = _DARK[(file, rank)]
                    except KeyError as exc:
                        raise FenCompatibilityError(
                            f"unrevealed {char} outside its starting square at {file},{rank}"
                        ) from exc
                    if char.isupper() != expected.isupper():
                        raise FenCompatibilityError(
                            f"unrevealed side mismatch at {file},{rank}"
                        )
                    output.append(expected)
                else:
                    output.append(char)
                file += 1
                continue
            raise FenCompatibilityError(f"invalid board character {char!r}")
        if file != 9:
            raise FenCompatibilityError(f"rank {row_index} has width {file}, expected 9")
        converted.append("".join(output))
    return "/".join(converted)


def _validate_inventory(inventory: str) -> None:
    if inventory == "":
        return
    pairs = list(_INVENTORY_PAIR.finditer(inventory))
    if not pairs or "".join(m.group(0) for m in pairs) != inventory:
        raise FenCompatibilityError("inventory must contain piece/single-digit pairs")
    seen: set[str] = set()
    for match in pairs:
        piece = match.group(1)
        if piece not in _INVENTORY_PIECES:
            raise FenCompatibilityError(f"unsupported inventory piece {piece!r}")
        if piece in seen:
            raise FenCompatibilityError(f"duplicate inventory piece {piece!r}")
        seen.add(piece)


def adapt_position_line(line: str) -> str:
    """Convert one GUI ``position fen`` line, preserving all other commands."""
    fields = line.strip().split()
    if len(fields) < 2 or fields[0].casefold() != "position":
        return line
    if fields[1].casefold() != "fen":
        return line
    # Native PF lines already have the marker between board and side.  Return
    # before validating field count so an opaque native command is untouched.
    if len(fields) >= 4 and fields[3].startswith("$"):
        return line
    if len(fields) < 4:
        raise FenCompatibilityError("position fen line is incomplete")
    board, side = fields[2:4]
    if side not in {"w", "b"}:
        raise FenCompatibilityError(f"invalid side {side!r}")
    # The GUI omits the inventory token after every hidden piece is revealed;
    # in that form the first tail token is the castling placeholder.
    if len(fields) >= 5 and fields[4] == "-":
        inventory = ""
        tail_start = 4
    elif len(fields) >= 5:
        inventory = fields[4]
        tail_start = 5
    else:
        raise FenCompatibilityError("position fen line is incomplete")
    if len(fields) < tail_start + 4:
        raise FenCompatibilityError("position fen line is incomplete")
    if fields[tail_start] != "-" or fields[tail_start + 1] != "-":
        raise FenCompatibilityError("unsupported castling or en-passant fields")
    if (not re.fullmatch(r"[0-9]+", fields[tail_start + 2])
            or not re.fullmatch(r"[0-9]+", fields[tail_start + 3])):
        raise FenCompatibilityError("halfmove/fullmove fields must be decimal")
    extra = fields[tail_start + 4:]
    if extra and extra[0].casefold() != "moves":
        raise FenCompatibilityError("position tail must begin with moves")
    _validate_inventory(inventory)
    converted = [
        "position", "fen", _board_to_pf(board), "$" + inventory, side,
        fields[tail_start], fields[tail_start + 1],
        fields[tail_start + 2], fields[tail_start + 3], *extra,
    ]
    return " ".join(converted)

