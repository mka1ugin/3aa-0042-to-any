#!/usr/bin/env python3
"""Редактор таблицы констант ABS TRW 450 в контейнере SGO.

Интерфейс на Flet. Раскладка полей и смещение таблицы зависят от прошивки.
Справка читается из HELP.md.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import flet as ft

import sgo
from record_layout import (
    CODING_FIELDS,
    KIND_CHOICE,
    KIND_ENUM,
    KIND_UINT,
    KIND_VOLUME,
    Field,
    bind_choices,
)
from reference_header import match_firmware, supported_firmware_names

APP_TITLE = "VAG TRW450 to any"
DEFAULT_SGO = Path(r"D:\YandexDisk\VAG\ABS\SGO\3AA698109__0042.sgo")

TABLE_START = 0x3A31C
TABLE_END = 0x3A6CD
RECORD_SIZE = 22
RECORD_COUNT = (TABLE_END - TABLE_START + 1) // RECORD_SIZE
FIELDS = CODING_FIELDS

BG = "#071422"
BG_CELL = "#0c2748"
BG_ALT = "#14365f"
BG_HEADER = "#050e22"
BG_MENU = "#071422"
MENU_POPUP = "#d4ecfb"
BG_MOD = "#3d4a22"
BG_OPEN = "#ff9a1f"
LINE = "#5d7ea3"
FG = "#1c2430"
FG_DIM = "#5c6770"
FG_ON_DARK = "#ffffff"
ROW_INK = "#f4f7fb"
ROW_INK_DIM = "#c5d2e0"
DIALOG_BG = "#f7f9fc"
CRC_TRW = "#0f5132"
CRC_BLOCK = "#146c43"
CRC_FILE = "#0b3d28"
CRC_BAR = "#e5f2e8"
CELL_FONT = 10
HEADER_FONT = 10
ROW_H = 29
CELL_PAD = 2
SCROLL_GUTTER = 14
CHROME_H = 108


@dataclass
class Change:
    record_index: int
    field_index: int
    old: bytes
    new: bytes


def parse_field_text(field: Field, text: str, extra: set[int] | None = None) -> bytes | None:
    if field.kind in {KIND_UINT, KIND_VOLUME}:
        cleaned = text.strip().replace(" ", "")
        if not cleaned.isdigit():
            return None
        number = int(cleaned)
        if field.kind == KIND_VOLUME:
            number = (number + 50) // 100
            if number > 0xFF:
                return None
            return bytes([number])
        limit = 1 << (8 * field.size)
        if number >= limit:
            return None
        return number.to_bytes(field.size, "big")

    raw = text.strip()
    if field.kind == KIND_ENUM:
        for value, label in field.labels:
            if raw == label:
                return bytes([value])
    token = "".join(raw.split()).upper()
    if token.startswith("0X"):
        token = token[2:]
    if not token or len(token) > field.size * 2:
        return None
    if any(char not in "0123456789ABCDEF" for char in token):
        return None
    try:
        value = int(token, 16)
    except ValueError:
        return None
    allowed = set(extra or ())
    if field.kind == KIND_CHOICE:
        allowed.update(field.choices)
        if value not in allowed:
            return None
    if field.kind == KIND_ENUM:
        allowed.update(item for item, _label in field.labels)
        if value not in allowed:
            return None
    try:
        return value.to_bytes(field.size, "big")
    except OverflowError:
        return None


def parse_hex_bytes(text: str, size: int) -> bytes | None:
    cleaned = "".join(text.strip().split()).lower()
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    if not cleaned or len(cleaned) > size * 2:
        return None
    if any(char not in "0123456789abcdef" for char in cleaned):
        return None
    try:
        return bytes.fromhex(cleaned.zfill(size * 2))
    except ValueError:
        return None


def _bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _asset_path(name: str) -> Path:
    return _bundle_root() / "assets" / name


def help_path() -> Path:
    bundled = _bundle_root() / "HELP.md"
    if bundled.is_file():
        return bundled
    return Path(__file__).resolve().parent / "HELP.md"


def read_help() -> str:
    path = help_path()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.read_text(encoding="utf-8")


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(data)
        try:
            temporary.replace(path)
        except OSError:
            path.write_bytes(data)
            temporary.unlink(missing_ok=True)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _format_stored(field: Field, stored: bytes) -> str:
    record = bytearray(field.offset + field.size)
    record[field.offset : field.offset + field.size] = stored
    return field.format(record)


def _invalid_message(field: Field) -> str:
    if field.kind == KIND_VOLUME:
        return "Введите объём в см³, например 1400 или 3600. Значение округляется до 100."
    if field.kind == KIND_UINT:
        limit = (1 << (8 * field.size)) - 1
        return f"Введите целое число от 0 до {limit}."
    if field.kind == KIND_ENUM:
        return "Выберите тип трансмиссии из списка."
    return "Выберите значение из списка."


def _format_change(current: int, original: int, digits: int) -> str:
    if current == original:
        return f"0x{current:0{digits}X} — без изменений"
    return f"0x{original:0{digits}X} → 0x{current:0{digits}X}"


def column_widths(fields: tuple[Field, ...]) -> tuple[int, ...]:
    """Одна ширина на колонку. Её получают и заголовок, и ячейки."""
    widths: list[int] = []
    for field in fields:
        if field.kind == KIND_ENUM and field.labels:
            text = max((label for _value, label in field.labels), key=len)
            widths.append(round(max(62, (len(text) * 7 + 40) // 2) * 1.4 * 1.2 * 1.2))
        elif field.kind == KIND_CHOICE:
            widths.append(round(34 * 1.4))
        elif field.kind in {KIND_UINT, KIND_VOLUME}:
            widths.append(42)
        else:
            widths.append(34)
    return tuple(widths)


def header_height(fields: tuple[Field, ...]) -> int:
    longest = max((len(field.name) for field in fields), default=1)
    return int((longest * 6 + 30) * 0.8)


def table_width(fields: tuple[Field, ...]) -> int:
    return sum(column_widths(fields)) + 1


class _Label:
    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text


class _Color:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _Brush:
    def __init__(self, name: str) -> None:
        self._name = name

    def color(self) -> _Color:
        return _Color(self._name)


class _Cell:
    def __init__(self) -> None:
        self._text = ""
        self._bg = BG_CELL

    def text(self) -> str:
        return self._text

    def background(self) -> _Brush:
        return _Brush(self._bg)


class _HeaderItem:
    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class _Header:
    def __init__(self, editor: Editor) -> None:
        self.editor = editor

    def count(self) -> int:
        return len(self.editor.fields)

    def height(self) -> int:
        return header_height(self.editor.fields)

    def sectionSize(self, index: int) -> int:
        return self.editor.column_widths[index]


class _Table:
    def __init__(self, editor: Editor) -> None:
        self.editor = editor
        self._items: list[list[_Cell]] = []

    def rowCount(self) -> int:
        return len(self._items)

    def columnCount(self) -> int:
        return len(self.editor.fields)

    def item(self, row: int, column: int) -> _Cell:
        return self._items[row][column]

    def columnWidth(self, index: int) -> int:
        return self.editor.column_widths[index]

    def horizontalHeader(self) -> _Header:
        return self.editor.header

    def horizontalHeaderItem(self, index: int) -> _HeaderItem:
        return _HeaderItem(self.editor.fields[index].name)


class Editor:
    """Модель открытой прошивки. Окно Flet только показывает её."""

    def __init__(self) -> None:
        self.dialogs = True
        self._last_error = ""
        self.warning = ""
        self.status_message = ""
        self.document: sgo.SgoDocument | None = None
        self.profile = None
        self.fields: tuple[Field, ...] = CODING_FIELDS
        self.block: sgo.Datablock | None = None
        self.path: Path | None = None
        self.image = bytearray()
        self.baseline = bytearray()
        self.raw_baseline: bytes | None = None
        self.original_bin_checksum = 0
        self.original_block_checksum = 0
        self.original_file_checksum = 0
        self.undo_stack: list[Change] = []
        self.redo_stack: list[Change] = []
        self.opened_rows: set[int] = set()
        self.table_text = ""
        self.header = _Header(self)
        self.table = _Table(self)
        self.crc_trw = _Label("CRC TRW  —")
        self.crc_block = _Label("CRC SGO block  —")
        self.crc_file = _Label("CRC SGO file  —")

    def close(self) -> None:
        return None

    @property
    def column_widths(self) -> tuple[int, ...]:
        return column_widths(self.fields)

    def window_title(self) -> str:
        if self.path is None:
            return APP_TITLE
        mark = "*" if self.is_dirty() else ""
        return f"{APP_TITLE} — {self.path.name}{mark}"

    def is_dirty(self) -> bool:
        if self.document is None:
            return False
        raw_dirty = self.raw_baseline is not None and self.document.raw != self.raw_baseline
        return self.image != self.baseline or raw_dirty

    def _record(self, index: int) -> bytes:
        start = self.profile.table_start + index * self.profile.record_size
        return bytes(self.image[start : start + self.profile.record_size])

    def _baseline_record(self, index: int) -> bytes:
        start = self.profile.table_start + index * self.profile.record_size
        return bytes(self.baseline[start : start + self.profile.record_size])

    def _fill_table(self) -> None:
        count = self.profile.record_count if self.profile is not None else 0
        self.table._items = [[_Cell() for _ in self.fields] for _ in range(count)]
        for row in range(count):
            self._refresh_row(row)

    def _refresh_row(self, row: int) -> None:
        record = self._record(row)
        modified = record != self._baseline_record(row)
        if row in self.opened_rows:
            background = BG_OPEN
        elif modified:
            background = BG_MOD
        else:
            background = BG_ALT if row % 2 else BG_CELL
        for column, field in enumerate(self.fields):
            cell = self.table.item(row, column)
            cell._text = field.format(record)
            cell._bg = background

    def _refresh_checksums(self) -> None:
        if self.document is None or self.block is None:
            return
        info = sgo.calc_bin_checksum(self.image)
        block_checksum = sgo.calculated_block_checksum(self.image)
        _packed, _bin, _block, file_checksum = sgo.build_edited_sgo(self.document, self.block, self.image)
        self.crc_trw.setText("CRC TRW  " + _format_change(info["actual"], self.original_bin_checksum, 4))
        self.crc_block.setText("CRC SGO block  " + _format_change(block_checksum, self.original_block_checksum, 4))
        self.crc_file.setText("CRC SGO file  " + _format_change(file_checksum, self.original_file_checksum, 8))

    def apply_edit(self, row: int, column: int, text: str) -> None:
        field = self.fields[column]
        current = field.read(self._record(row))
        value = parse_field_text(field, text, {current})
        if value is None:
            self.status_message = _invalid_message(field)
            self._refresh_row(row)
            return
        self._write_field(row, column, value, record_history=True)

    def _write_field(self, row: int, column: int, value: bytes, record_history: bool) -> bool:
        field = self.fields[column]
        if not field.editable:
            return False
        offset = self.profile.table_start + row * self.profile.record_size + field.offset
        old = bytes(self.image[offset : offset + field.size])
        if old == value:
            self._refresh_row(row)
            return False
        if record_history:
            self.undo_stack.append(Change(row, column, old, value))
            self.redo_stack.clear()
        self.image[offset : offset + field.size] = value
        sgo.apply_bin_checksum(self.image)
        self._refresh_row(row)
        self._refresh_checksums()
        self.status_message = (
            f"Запись {row + 1}, «{field.name}»: {_format_stored(field, old)} → {_format_stored(field, value)}"
        )
        return True

    def _undo(self) -> None:
        if not self.undo_stack:
            return
        change = self.undo_stack.pop()
        if not self._write_field(change.record_index, change.field_index, change.old, record_history=False):
            self.undo_stack.append(change)
            return
        self.redo_stack.append(change)

    def _redo(self) -> None:
        if not self.redo_stack:
            return
        change = self.redo_stack.pop()
        if not self._write_field(change.record_index, change.field_index, change.new, record_history=False):
            self.redo_stack.append(change)
            return
        self.undo_stack.append(change)

    def revert_edits(self) -> None:
        if self.document is None or not self.is_dirty():
            return
        self.image = bytearray(self.baseline)
        if self.raw_baseline is not None and self.document.raw != self.raw_baseline:
            self.document = sgo.parse_sgo(self.raw_baseline)
            self.block = sgo.calibration_block(self.document, self.profile.block_index)
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.opened_rows.clear()
        self.status_message = ""
        self._fill_table()
        self._refresh_checksums()

    def recalculate_checksums(self) -> str:
        if self.document is None or self.block is None:
            raise ValueError("Сначала откройте файл SGO.")
        before = self.document.raw
        corrected = sgo.recalculate_all_checksums(before, {self.block.index: bytes(self.image)})
        document = sgo.parse_sgo(corrected)
        block = sgo.calibration_block(document, self.profile.block_index)
        image = sgo.decode_block(document, block)
        lines = []
        for old, new in zip(sgo.parse_sgo(before).blocks, document.blocks):
            old_image = sgo.decode_block(sgo.parse_sgo(before), old) if old.index != self.block.index else bytearray(self.image)
            if sgo.is_bin_image(old_image):
                old_bin = sgo.calc_bin_checksum(old_image)["actual"]
                new_bin = sgo.calc_bin_checksum(sgo.decode_block(document, new))["actual"]
                if old_bin != new_bin:
                    lines.append(f"CRC BIN блока №{new.index}: 0x{old_bin:04X} → 0x{new_bin:04X}")
            if old.checksum != new.checksum:
                lines.append(f"CRC SGO блока №{new.index}: 0x{old.checksum:04X} → 0x{new.checksum:04X}")
        old_file = sgo.stored_file_checksum(before)
        new_file = sgo.stored_file_checksum(corrected)
        if old_file != new_file:
            lines.append(f"CRC SGO file: 0x{old_file:08X} → 0x{new_file:08X}")
        self.document = document
        self.block = block
        self.image = image
        self._fill_table()
        self._refresh_checksums()
        if not lines:
            self.status_message = "Все контрольные суммы уже верные."
            return self.status_message
        self.status_message = "Контрольные суммы пересчитаны. Сохраните файл."
        return self.status_message + "\n\n" + "\n".join(lines)

    def mark_saved(self, bin_checksum: int, block_checksum: int, file_checksum: int, target: Path, packed: bytes) -> None:
        self.document = sgo.parse_sgo(packed)
        self.block = sgo.calibration_block(self.document, self.profile.block_index)
        self.raw_baseline = self.document.raw
        self.baseline = bytearray(self.image)
        self.original_bin_checksum = bin_checksum
        self.original_block_checksum = block_checksum
        self.original_file_checksum = file_checksum
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.path = target
        self.status_message = f"Сохранено: {target}"
        self._fill_table()
        self._refresh_checksums()

    def load_file(self, path: Path) -> bool:
        try:
            document = sgo.parse_sgo(path)
            profile = match_firmware(sgo.container_header(document.raw))
            if profile is None:
                raise ValueError(
                    "Заголовок SGO не совпадает с поддерживаемыми прошивками.\n"
                    "Контрольные суммы при сравнении не учитываются.\n\n"
                    f"Поддерживаются:\n{supported_firmware_names()}"
                )
            block = sgo.calibration_block(document, profile.block_index)
            image = sgo.decode_block(document, block)
            if profile.table_end >= len(image):
                raise ValueError("Таблица не помещается в блок прошивки")
            bin_info = sgo.calc_bin_checksum(image)
            file_checksum = sgo.stored_file_checksum(document.raw)
            file_calculated = sgo.calculated_file_checksum(document.raw)
            block_calculated = sgo.calculated_block_checksum(image)
        except (OSError, ValueError) as exc:
            self._last_error = str(exc)
            return False

        self.document = document
        self.profile = profile
        self.block = block
        self.path = path
        self.image = image
        self.baseline = bytearray(image)
        self.raw_baseline = document.raw
        self.fields = bind_choices(
            profile.fields,
            image,
            profile.table_start,
            profile.record_size,
            profile.record_count,
        )
        self.original_bin_checksum = bin_info["actual"]
        self.original_block_checksum = block.checksum
        self.original_file_checksum = file_checksum
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.opened_rows.clear()
        self._last_error = ""
        self.status_message = ""
        self.table_text = (
            f"Таблица {profile.name}: bin 0x{profile.table_start:06X}–0x{profile.table_end:06X}, "
            f"{profile.record_count} записей по {profile.record_size} байта"
        )
        problems = []
        if bin_info["actual"] != bin_info["calculated"] or bin_info["total_16"] != 0:
            problems.append(f"CRC TRW: в файле 0x{bin_info['actual']:04X}, расчёт 0x{bin_info['calculated']:04X}")
        if block.checksum != block_calculated:
            problems.append(f"CRC SGO block: в файле 0x{block.checksum:04X}, расчёт 0x{block_calculated:04X}")
        if file_checksum != file_calculated:
            problems.append(f"CRC SGO file: в файле 0x{file_checksum:08X}, расчёт 0x{file_calculated:08X}")
        self.warning = ("\n".join(problems) + "\n\nПри сохранении они будут пересчитаны.") if problems else ""
        self._fill_table()
        self._refresh_checksums()
        return True


def _cell_border() -> ft.Border:
    side = ft.BorderSide(width=1, color=LINE)
    return ft.Border.only(right=side, bottom=side)


def _ink(background: str, editable: bool) -> str:
    if background == BG_OPEN:
        return FG if editable else FG_DIM
    if background in {BG_CELL, BG_ALT}:
        return ROW_INK if editable else ROW_INK_DIM
    return FG_ON_DARK if editable else "#d9d3c3"


def _input_colors(background: str, editable: bool) -> dict:
    return {
        "color": _ink(background, editable),
        "bgcolor": background,
        "fill_color": background,
        "border_color": background,
        "focused_border_color": LINE,
        "text_size": CELL_FONT,
        "dense": True,
        "content_padding": ft.Padding.symmetric(horizontal=2, vertical=0),
    }


def build_grid(editor: Editor, on_choice, on_text, on_open=None) -> tuple[ft.Row, list[ft.Row]]:
    """Заголовок и строки. Ширина колонки задаётся одним списком."""
    if on_open is None:
        on_open = lambda _row: None
    widths = editor.column_widths
    height = header_height(editor.fields)
    header_cells: list[ft.Control] = []
    for field, width in zip(editor.fields, widths):
        header_cells.append(
            ft.Container(
                width=width,
                height=height,
                bgcolor=BG_HEADER,
                border=_cell_border(),
                alignment=ft.Alignment.CENTER,
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                tooltip=field.name,
                content=ft.RotatedBox(
                    quarter_turns=3,
                    content=ft.Text(
                        field.name,
                        size=HEADER_FONT,
                        color="#ffffff",
                        weight=ft.FontWeight.BOLD,
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=True,
                    ),
                ),
            )
        )
    header = ft.Row(spacing=0, controls=header_cells, tight=True)

    rows: list[ft.Row] = []
    for row in range(editor.table.rowCount()):
        cells: list[ft.Control] = []
        for column, (field, width) in enumerate(zip(editor.fields, widths)):
            text = editor.table.item(row, column).text()
            background = editor.table.item(row, column).background().color().name()
            inner_w = width - CELL_PAD * 2
            inner_h = ROW_H - CELL_PAD * 2
            if not field.editable:
                control: ft.Control = ft.Text(
                    text,
                    size=CELL_FONT,
                    color=_ink(background, False),
                    text_align=ft.TextAlign.CENTER,
                    font_family="Consolas",
                )
            elif field.kind in {KIND_CHOICE, KIND_ENUM}:
                control = ft.Dropdown(
                    value=text,
                    options=[
                        ft.DropdownOption(
                            key=label,
                            text=label,
                            style=ft.ButtonStyle(
                                text_style=ft.TextStyle(size=CELL_FONT, font_family="Consolas", color=FG),
                                padding=ft.Padding.symmetric(horizontal=4, vertical=0),
                            ),
                        )
                        for label in field.choices_for(field.read(editor._record(row)))
                    ],
                    width=inner_w,
                    height=inner_h,
                    text_align=ft.TextAlign.CENTER,
                    text_style=ft.TextStyle(size=CELL_FONT, font_family="Consolas", color=_ink(background, True)),
                    menu_style=ft.MenuStyle(
                        bgcolor="#fff6ea",
                        padding=ft.Padding.symmetric(horizontal=2, vertical=0),
                    ),
                    filled=True,
                    border=ft.NoInputBorder(),
                    on_focus=lambda e, row=row: on_open(row),
                    on_select=lambda e, row=row, column=column: on_choice(row, column, e.control.value or ""),
                    **_input_colors(background, True),
                )
            else:
                control = ft.TextField(
                    value=text,
                    width=inner_w,
                    height=inner_h,
                    text_align=ft.TextAlign.CENTER,
                    filled=True,
                    border=ft.NoInputBorder(),
                    keyboard_type=ft.KeyboardType.NUMBER,
                    on_focus=lambda e, row=row: on_open(row),
                    on_submit=lambda e, row=row, column=column: on_text(row, column, e.control.value or ""),
                    on_blur=lambda e, row=row, column=column: on_text(row, column, e.control.value or ""),
                    **_input_colors(background, True),
                )
            cells.append(
                ft.Container(
                    width=width,
                    height=ROW_H,
                    padding=CELL_PAD,
                    bgcolor=background,
                    border=_cell_border(),
                    alignment=ft.Alignment.CENTER,
                    content=control,
                )
            )
        rows.append(ft.Row(spacing=0, controls=cells, tight=True))
    return header, rows


class FletEditor:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.editor = Editor()
        self._closing = False
        self.picker = ft.FilePicker()
        self.page.services.append(self.picker)
        self.crc_trw = ft.Text(self.editor.crc_trw.text(), color=CRC_TRW, size=12, font_family="Consolas")
        self.crc_block = ft.Text(self.editor.crc_block.text(), color=CRC_BLOCK, size=12, font_family="Consolas")
        self.crc_file = ft.Text(self.editor.crc_file.text(), color=CRC_FILE, size=12, font_family="Consolas")
        self.status = ft.Text("", color=ROW_INK_DIM, size=11)
        self.header_host = ft.Row(spacing=0, tight=True)
        self.body = ft.ListView(
            spacing=0,
            padding=ft.Padding.only(right=SCROLL_GUTTER),
            scroll=ft.Scrollbar(
                thumb_visibility=True,
                track_visibility=True,
                thickness=12,
                interactive=True,
            ),
        )
        self.table_box = ft.Container(
            border=ft.Border.only(
                left=ft.BorderSide(width=1, color=LINE),
                top=ft.BorderSide(width=1, color=LINE),
            ),
            content=ft.Column(spacing=0, tight=True, controls=[self.header_host, self.body]),
        )
        self.table_scroll = ft.Row(
            scroll=ft.ScrollMode.AUTO,
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[self.table_box],
        )
        self.save_item = self._item("Сохранить SGO...", self.save_as)
        self.undo_item = self._item("Отменить", self.undo)
        self.redo_item = self._item("Повторить", self.redo)
        self.revert_item = self._item("Отменить все правки", self.revert)
        self.recalc_item = self._item("Пересчитать все контрольные суммы", self.recalculate_checksums)
        popup = ft.MenuStyle(
            bgcolor=MENU_POPUP,
            side=ft.BorderSide(width=1, color="#7eb6d9"),
        )
        menu = ft.MenuBar(
            style=ft.MenuStyle(bgcolor=BG_MENU),
            controls=[
                ft.SubmenuButton(
                    content=ft.Text("Файл", color=FG_ON_DARK, size=13),
                    menu_style=popup,
                    controls=[
                        self._item("Открыть SGO...", self.open_file),
                        self.save_item,
                        ft.MenuItemButton(content=ft.Text("Выход", color=FG, size=13), on_click=lambda e: self.page.run_task(self.close_requested)),
                    ],
                ),
                ft.SubmenuButton(
                    content=ft.Text("Правка", color=FG_ON_DARK, size=13),
                    menu_style=popup,
                    controls=[self.undo_item, self.redo_item, self.revert_item, self.recalc_item],
                ),
                ft.SubmenuButton(
                    content=ft.Text("Справка", color=FG_ON_DARK, size=13),
                    menu_style=popup,
                    controls=[self._item("Справка", self.show_help)],
                ),
            ],
        )
        self.root = ft.Column(
            expand=True,
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Container(bgcolor=BG_MENU, content=menu),
                ft.Container(expand=True, content=self.table_scroll),
                ft.Container(
                    bgcolor=CRC_BAR,
                    padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                    content=ft.Row(
                        controls=[
                            ft.Container(expand=True, content=self.crc_trw),
                            ft.Container(expand=True, content=self.crc_block),
                            ft.Container(expand=True, content=self.crc_file),
                        ],
                    ),
                ),
                ft.Container(
                    bgcolor=BG,
                    padding=ft.Padding.symmetric(horizontal=12, vertical=4),
                    content=self.status,
                ),
            ],
        )

    def _item(self, title: str, handler) -> ft.MenuItemButton:
        async def invoke() -> None:
            result = handler()
            if asyncio.iscoroutine(result):
                await result

        return ft.MenuItemButton(
            content=ft.Text(title, color=FG, size=13),
            on_click=lambda e: self.page.run_task(invoke),
        )

    def rebuild(self) -> None:
        header, rows = build_grid(self.editor, self.commit_choice, self.commit_text, self.open_row)
        width = sum(self.editor.column_widths)
        self.header_host.controls = [
            *header.controls,
            ft.Container(width=SCROLL_GUTTER, height=header_height(self.editor.fields), bgcolor=BG_HEADER),
        ]
        self.header_host.width = width + SCROLL_GUTTER
        self.body.width = width + SCROLL_GUTTER
        self.table_box.width = width + SCROLL_GUTTER + 1
        if rows:
            self.body.controls = rows
        else:
            self.body.controls = [
                ft.Container(
                    width=420,
                    height=80,
                    padding=16,
                    content=ft.Text("Откройте SGO через меню «Файл».", color=FG_DIM, size=12),
                )
            ]
        self.layout_body()
        self.sync_chrome()

    def content_width(self) -> int:
        return sum(self.editor.column_widths) + SCROLL_GUTTER + 1

    def layout_body(self, width: float | None = None, height: float | None = None) -> None:
        height = height or self.page.height or self.page.window.height or 860
        self.body.height = max(180, int(height) - CHROME_H - header_height(self.editor.fields))

    def on_page_resize(self, event: ft.PageResizeEvent) -> None:
        self.layout_body(event.width, event.height)
        self.page.update()

    def fit_window(self) -> None:
        width = self.content_width()
        rows = max(self.editor.table.rowCount(), 8)
        height = min(980, CHROME_H + header_height(self.editor.fields) + min(rows, 16) * ROW_H)
        self.page.window.width = width
        self.page.window.height = max(560, height)
        self.page.window.min_width = width
        self.layout_body()

    def show_startup(self) -> None:
        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                bgcolor=DIALOG_BG,
                title=ft.Text(APP_TITLE, color=FG, weight=ft.FontWeight.BOLD),
                content=ft.Text(
                    "Навайбкодил М. Калугин в 2026 году.\n\n"
                    f"Поддерживаемые прошивки:\n{supported_firmware_names()}\n\n"
                    "Работа с прошивкой 3C0 не гарантируется. "
                    "У 3C0698109__0017 другой расчёт контрольной суммы BIN: "
                    "она охватывает не весь блок.",
                    color=FG,
                ),
                actions=[ft.TextButton("Закрыть", on_click=lambda e: self.page.pop_dialog())],
            )
        )

    def sync_chrome(self) -> None:
        self.crc_trw.value = self.editor.crc_trw.text()
        self.crc_block.value = self.editor.crc_block.text()
        self.crc_file.value = self.editor.crc_file.text()
        self.status.value = self.editor.status_message or self.editor.table_text
        self.page.title = self.editor.window_title()
        loaded = self.editor.document is not None
        dirty = self.editor.is_dirty()
        self.save_item.disabled = not loaded
        self.undo_item.disabled = not self.editor.undo_stack
        self.redo_item.disabled = not self.editor.redo_stack
        self.revert_item.disabled = not dirty
        self.recalc_item.disabled = not loaded

    def sync_row(self, row: int) -> None:
        if row >= len(self.body.controls):
            return
        line = self.body.controls[row]
        if not isinstance(line, ft.Row):
            return
        for column, cell in enumerate(line.controls):
            if not isinstance(cell, ft.Container):
                continue
            background = self.editor.table.item(row, column).background().color().name()
            text = self.editor.table.item(row, column).text()
            cell.bgcolor = background
            control = cell.content
            ink = _ink(background, True)
            if isinstance(control, (ft.Dropdown, ft.TextField)):
                if control.value != text:
                    control.value = text
                control.color = ink
                style = getattr(control, "text_style", None)
                if style is not None:
                    style.color = ink
                control.bgcolor = background
                control.fill_color = background
                control.border_color = background
            elif isinstance(control, ft.Text):
                control.value = text
                control.color = _ink(background, False)

    def open_row(self, row: int) -> None:
        if row in self.editor.opened_rows:
            return
        self.editor.opened_rows.add(row)
        self.editor._refresh_row(row)
        self.sync_row(row)
        self.page.update()

    def commit_choice(self, row: int, column: int, text: str) -> None:
        current = self.editor.table.item(row, column).text()
        self.editor.opened_rows.add(row)
        if text == current:
            self.editor._refresh_row(row)
            self.sync_row(row)
            self.page.update()
            return
        self.editor.apply_edit(row, column, text)
        self.sync_row(row)
        self.sync_chrome()
        self.page.update()

    def commit_text(self, row: int, column: int, text: str) -> None:
        current = self.editor.table.item(row, column).text()
        self.editor.opened_rows.add(row)
        if text.strip() == current:
            self.editor._refresh_row(row)
            self.sync_row(row)
            self.page.update()
            return
        self.editor.apply_edit(row, column, text)
        self.sync_row(row)
        self.sync_chrome()
        self.page.update()

    def undo(self) -> None:
        if not self.editor.undo_stack:
            return
        row = self.editor.undo_stack[-1].record_index
        self.editor._undo()
        self.sync_row(row)
        self.sync_chrome()
        self.page.update()

    def redo(self) -> None:
        if not self.editor.redo_stack:
            return
        row = self.editor.redo_stack[-1].record_index
        self.editor._redo()
        self.sync_row(row)
        self.sync_chrome()
        self.page.update()

    async def recalculate_checksums(self) -> None:
        if self.editor.document is None:
            return
        try:
            report = self.editor.recalculate_checksums()
        except (OSError, ValueError) as exc:
            await self.message("Ошибка", str(exc))
            return
        self.rebuild()
        self.page.update()
        await self.message("Контрольные суммы", report)

    async def revert(self) -> None:
        if self.editor.document is None or not self.editor.is_dirty():
            return
        if self.editor.dialogs:
            agreed = await self.ask(
                "Отменить правки",
                "Вернуть таблицу к последнему открытому или сохранённому состоянию?",
            )
            if not agreed:
                return
        self.editor.revert_edits()
        self.rebuild()
        self.page.update()

    async def open_file(self) -> None:
        if not await self.confirm_discard():
            return
        folder = None
        if self.editor.path is not None:
            folder = str(self.editor.path.parent)
        elif DEFAULT_SGO.parent.is_dir():
            folder = str(DEFAULT_SGO.parent)
        files = await self.picker.pick_files(
            dialog_title="Открыть SGO",
            initial_directory=folder,
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["sgo"],
            allow_multiple=False,
        )
        if not files or not files[0].path:
            return
        self._load(Path(files[0].path))

    async def save_as(self) -> bool:
        if self.editor.document is None or self.editor.block is None or self.editor.path is None:
            return False
        try:
            sgo.apply_bin_checksum(self.editor.image)
            packed, bin_checksum, block_checksum, file_checksum = sgo.build_edited_sgo(
                self.editor.document,
                self.editor.block,
                self.editor.image,
            )
        except (OSError, ValueError) as exc:
            await self.message("Ошибка", f"Не удалось сохранить файл:\n{exc}")
            return False
        selected = await self.picker.save_file(
            dialog_title="Сохранить SGO",
            file_name=f"{self.editor.path.stem}_edited.sgo",
            initial_directory=str(self.editor.path.parent),
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["sgo"],
            src_bytes=packed,
        )
        if not selected:
            return False
        target = Path(selected)
        self.editor.mark_saved(bin_checksum, block_checksum, file_checksum, target, packed)
        self.rebuild()
        self.page.update()
        await self.message(
            "Сохранено",
            f"Файл сохранён:\n{target}\n\n"
            f"CRC TRW: 0x{bin_checksum:04X}\n"
            f"CRC SGO block: 0x{block_checksum:04X}\n"
            f"CRC SGO file: 0x{file_checksum:08X}",
        )
        return True

    def _load(self, path: Path) -> None:
        if not self.editor.load_file(path):
            self.page.show_dialog(self._notice("Ошибка", self.editor._last_error))
            return
        self.fit_window()
        self.rebuild()
        self.page.update()
        if self.editor.warning and self.editor.dialogs:
            self.page.show_dialog(self._notice("Контрольные суммы", self.editor.warning))

    async def show_help(self) -> None:
        try:
            text = read_help()
        except OSError as exc:
            await self.message("Справка", f"Не удалось открыть HELP.md:\n{exc}")
            return
        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                bgcolor=DIALOG_BG,
                title=ft.Text("Справка", color=FG),
                content=ft.Container(
                    width=760,
                    height=560,
                    content=ft.Column(
                        scroll=ft.ScrollMode.AUTO,
                        controls=[
                            ft.Markdown(
                                value=text,
                                selectable=True,
                                extension_set=ft.MarkdownExtensionSet.GITHUB_FLAVORED,
                                width=720,
                            )
                        ],
                    ),
                ),
                actions=[ft.TextButton("Закрыть", on_click=lambda e: self.page.pop_dialog())],
            )
        )

    def _notice(self, title: str, text: str) -> ft.AlertDialog:
        return ft.AlertDialog(
            modal=True,
            bgcolor=DIALOG_BG,
            title=ft.Text(title, color=FG),
            content=ft.Text(text, color=FG),
            actions=[ft.TextButton("Закрыть", on_click=lambda e: self.page.pop_dialog())],
        )

    async def message(self, title: str, text: str) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        def finish() -> None:
            if not future.done():
                future.set_result(None)

        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                bgcolor=DIALOG_BG,
                title=ft.Text(title, color=FG),
                content=ft.Text(text, color=FG),
                actions=[ft.TextButton("Закрыть", on_click=lambda e: (finish(), self.page.pop_dialog()))],
                on_dismiss=lambda e: finish(),
            )
        )
        await future

    async def ask(self, title: str, text: str) -> bool:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()

        def choose(value: bool) -> None:
            if not future.done():
                future.set_result(value)
            self.page.pop_dialog()

        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                bgcolor=DIALOG_BG,
                title=ft.Text(title, color=FG),
                content=ft.Text(text, color=FG),
                actions=[
                    ft.TextButton("Да", on_click=lambda e: choose(True)),
                    ft.TextButton("Нет", on_click=lambda e: choose(False)),
                ],
                on_dismiss=lambda e: future.done() or future.set_result(False),
            )
        )
        return await future

    async def confirm_discard(self) -> bool:
        if self.editor.document is None or self.editor.image == self.editor.baseline:
            return True
        if not self.editor.dialogs:
            return True
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()

        def choose(value: str) -> None:
            if not future.done():
                future.set_result(value)
            self.page.pop_dialog()

        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                bgcolor=DIALOG_BG,
                title=ft.Text("Несохранённые изменения", color=FG),
                content=ft.Text("Сохранить изменения перед продолжением?", color=FG),
                actions=[
                    ft.TextButton("Сохранить", on_click=lambda e: choose("save")),
                    ft.TextButton("Не сохранять", on_click=lambda e: choose("discard")),
                    ft.TextButton("Отмена", on_click=lambda e: choose("cancel")),
                ],
                on_dismiss=lambda e: future.done() or future.set_result("cancel"),
            )
        )
        answer = await future
        if answer == "cancel":
            return False
        if answer == "save":
            return await self.save_as()
        return True

    async def close_requested(self) -> None:
        if self._closing:
            return
        if not await self.confirm_discard():
            return
        self._closing = True
        await self.page.window.destroy()

    def on_window(self, event: ft.WindowEvent) -> None:
        if event.type == ft.WindowEventType.RESIZED:
            self.layout_body()
            self.page.update()
        elif event.type == ft.WindowEventType.CLOSE:
            self.page.run_task(self.close_requested)

    def on_key(self, event: ft.KeyboardEvent) -> None:
        if not event.ctrl or event.shift or event.alt:
            return
        key = event.key.lower()
        if key == "o":
            self.page.run_task(self.open_file)
        elif key == "s":
            self.page.run_task(self.save_as)
        elif key == "z":
            self.undo()
        elif key == "y":
            self.redo()


def flet_main(page: ft.Page) -> None:
    theme = ft.Theme(
        color_scheme_seed="#0c2748",
        surfaces=ft.ThemeSurfaces.TONAL,
        font_family="Segoe UI",
        scrollbar_theme=ft.ScrollbarTheme(
            thumb_visibility=True,
            track_visibility=True,
            thickness=12,
            thumb_color="#9eb6d4",
            track_color="#050e22",
            interactive=True,
        ),
        color_scheme=ft.ColorScheme(
            primary="#14365f",
            on_primary="#ffffff",
            surface="#0c2748",
            on_surface="#f4f7fb",
            surface_container="#071422",
            surface_container_high="#14365f",
            outline=LINE,
        ),
    )
    page.title = APP_TITLE
    page.theme_mode = ft.ThemeMode.LIGHT
    page.theme = theme
    page.dark_theme = theme
    page.bgcolor = BG
    page.padding = 0
    page.window.bgcolor = BG
    page.window.min_width = 480
    page.window.min_height = 480
    page.window.prevent_close = True
    icon = _asset_path("app.ico")
    if icon.is_file():
        page.window.icon = str(icon)
    view = FletEditor(page)
    page.window.on_event = view.on_window
    page.on_resize = view.on_page_resize
    page.on_keyboard_event = view.on_key
    page.add(view.root)
    if DEFAULT_SGO.exists():
        view.editor.dialogs = True
        if view.editor.load_file(DEFAULT_SGO):
            view.fit_window()
            view.rebuild()
            page.update()
            view.show_startup()
            if view.editor.warning:
                page.show_dialog(view._notice("Контрольные суммы", view.editor.warning))
        elif view.editor._last_error:
            view.show_startup()
            page.show_dialog(view._notice("Ошибка", view.editor._last_error))
    else:
        view.fit_window()
        view.rebuild()
        page.update()
        view.show_startup()


def main() -> None:
    assets = Path(__file__).resolve().parent / "assets"
    ft.run(flet_main, assets_dir=str(assets) if assets.is_dir() else None)


if __name__ == "__main__":
    main()
