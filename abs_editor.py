#!/usr/bin/env python3
"""Редактор таблицы констант ABS 3AA в контейнере SGO.

Запись — 22 байта. Раскладка полей задана списком FIELDS.
Смещение таблицы зависит от прошивки: его задаёт профиль в reference_header.
Многобайтовые числа хранятся big-endian.
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sgo
from record_layout import (
    CODING_FIELDS,
    KIND_CHOICE,
    KIND_ENUM,
    KIND_UINT,
    KIND_VOLUME,
    Field,
    bind_choices,
    layout_key,
)
from reference_header import match_firmware, supported_firmware_names

APP_TITLE = "VAG TRW450 to any"
DEFAULT_SGO = Path(r"D:\YandexDisk\VAG\ABS\SGO\3AA698109__0042.sgo")

TABLE_START = 0x3A31C
TABLE_END = 0x3A6CD
RECORD_SIZE = 22
RECORD_COUNT = (TABLE_END - TABLE_START + 1) // RECORD_SIZE

# Совместимость со старыми тестами: общая раскладка четырёх текущих прошивок.
FIELDS = CODING_FIELDS


@dataclass
class Change:
    record_index: int
    field_index: int
    old: bytes
    new: bytes


@dataclass
class Column:
    key: str
    title: str
    width: int
    field_index: int
    editable: bool


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


def ask_save_choice(parent: tk.Misc, message: str) -> str:
    dialog = tk.Toplevel(parent)
    dialog.title("Несохранённые изменения")
    dialog.transient(parent)
    dialog.resizable(False, False)
    dialog.grab_set()
    choice = {"value": "cancel"}

    frame = ttk.Frame(dialog, padding=16)
    frame.grid(row=0, column=0)
    ttk.Label(frame, text=message, wraplength=420, justify="left").grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 16)
    )

    def close(value: str) -> None:
        choice["value"] = value
        dialog.grab_release()
        dialog.destroy()

    ttk.Button(frame, text="Сохранить", command=lambda: close("save"), width=16).grid(
        row=1, column=0, padx=(0, 8)
    )
    ttk.Button(frame, text="Не сохранять", command=lambda: close("discard"), width=16).grid(
        row=1, column=1, padx=(0, 8)
    )
    ttk.Button(frame, text="Отмена", command=lambda: close("cancel"), width=12).grid(
        row=1, column=2
    )
    dialog.protocol("WM_DELETE_WINDOW", lambda: close("cancel"))
    dialog.update_idletasks()
    px = parent.winfo_rootx() + (parent.winfo_width() - dialog.winfo_width()) // 2
    py = parent.winfo_rooty() + (parent.winfo_height() - dialog.winfo_height()) // 2
    dialog.geometry(f"+{max(px, 0)}+{max(py, 0)}")
    dialog.wait_window()
    return choice["value"]


class Editor(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1240x920")
        self.minsize(900, 640)
        self._set_window_icon()
        self._configure_style()

        self.document: sgo.SgoDocument | None = None
        self.profile = None
        self.fields: tuple[Field, ...] = CODING_FIELDS
        self.block: sgo.Datablock | None = None
        self.path: Path | None = None
        self.image = bytearray()
        self.baseline = bytearray()
        self.original_bin_checksum = 0
        self.original_block_checksum = 0
        self.original_file_checksum = 0
        self.undo_stack: list[Change] = []
        self.redo_stack: list[Change] = []
        self._editor: tk.Widget | None = None
        self._editor_record = 0
        self._editor_field = 0
        self._editor_extra: set[int] = set()
        self._current_record: int | None = None
        self._current_field: int | None = None

        self.file_var = tk.StringVar(value="Файл не открыт")
        self.block_var = tk.StringVar(value="Блок №1: —")
        self.table_var = tk.StringVar(
            value=(
                f"Таблица: bin 0x{TABLE_START:06X}–0x{TABLE_END:06X}, "
                f"{RECORD_COUNT} записей по {RECORD_SIZE} байта"
            )
        )
        self.bin_cs_var = tk.StringVar(value="КС bin (смещение 0x0004): —")
        self.block_cs_var = tk.StringVar(value="КС блока №1: —")
        self.file_cs_var = tk.StringVar(value="КС файла SGO: —")
        self.dirty_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(
            value="Откройте SGO поддерживаемой версии: 3AA 0042, 3AA 0023, 3AA 0061, 5N0 0023, 5N0 0161 или 8U0 0406."
        )

        self._build_ui()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("Treeview", font=("Consolas", 10), rowheight=24)
        style.configure("Treeview.Heading", font=("Segoe UI", 9))
        style.configure("TButton", padding=(10, 4))
        style.map(
            "Treeview",
            background=[("selected", "#CCE4F7")],
            foreground=[("selected", "#000000")],
        )

        def fixed_map(option: str):
            return [
                item
                for item in style.map("Treeview", query_opt=option)
                if item[:2] != ("!disabled", "!selected")
            ]

        style.map(
            "Treeview",
            foreground=fixed_map("foreground"),
            background=fixed_map("background"),
        )

    def _build_columns(self) -> list[Column]:
        cell_font = tkfont.Font(family="Consolas", size=10)
        columns = []
        for index, field in enumerate(self.fields):
            if field.kind == KIND_ENUM and field.labels:
                width = max(cell_font.measure(label) for _value, label in field.labels) + 22
            elif field.kind in {KIND_UINT, KIND_VOLUME}:
                width = 68
            else:
                width = 48
            columns.append(Column(f"f{index}", field.name, width, index, field.editable))
        return columns

    def _install_fields(self, fields: tuple[Field, ...]) -> None:
        key = layout_key(fields)
        self.fields = fields
        if key != getattr(self, "_layout_key", None):
            self._layout_key = key
            self._mount_tree()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self._build_menu()

        info = ttk.Frame(self, padding=(12, 10, 12, 4))
        info.grid(row=0, column=0, sticky="ew")
        info.columnconfigure(0, weight=1)
        info.columnconfigure(1, weight=1)

        ttk.Label(info, textvariable=self.file_var).grid(row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.bin_cs_var).grid(row=0, column=1, sticky="w")
        ttk.Label(info, textvariable=self.block_var).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(info, textvariable=self.block_cs_var).grid(row=1, column=1, sticky="w", pady=(2, 0))
        ttk.Label(info, textvariable=self.table_var).grid(row=2, column=0, sticky="w", pady=(2, 0))
        ttk.Label(info, textvariable=self.file_cs_var).grid(row=2, column=1, sticky="w", pady=(2, 0))
        ttk.Label(info, textvariable=self.dirty_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )

        buttons = ttk.Frame(info)
        buttons.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.save_button = ttk.Button(buttons, text="Сохранить SGO...", command=self._save_as)
        self.save_button.grid(row=0, column=0, padx=(0, 8))
        self.reset_button = ttk.Button(
            buttons, text="Отменить все правки", command=self._revert_all
        )
        self.reset_button.grid(row=0, column=1)

        hint = (
            "Двойной щелчок, F2 или Enter — правка. Списки берутся из открытой прошивки. "
            "Мощность и масса — десятичные числа, объём двигателя — в см³ с округлением до 100. "
            "Поля только для чтения не меняются."
        )
        ttk.Label(self, text=hint, wraplength=1200, padding=(12, 0, 12, 6), foreground="#444444").grid(
            row=1, column=0, sticky="ew"
        )

        table = ttk.Frame(self, padding=(12, 0, 12, 0))
        table.grid(row=2, column=0, sticky="nsew")
        table.rowconfigure(1, weight=1)
        table.columnconfigure(0, weight=1)

        self._table = table
        self._header_font = tkfont.Font(family="Segoe UI", size=9)
        self.header = tk.Canvas(
            table,
            height=40,
            highlightthickness=0,
            borderwidth=0,
            bg="#E4E4E4",
        )
        self.header.grid(row=0, column=0, sticky="ew")
        self.y_scroll = ttk.Scrollbar(table, orient="vertical")
        self.x_scroll = ttk.Scrollbar(table, orient="horizontal", command=self._xview)
        self.y_scroll.grid(row=1, column=1, sticky="ns")
        self.x_scroll.grid(row=2, column=0, sticky="ew")
        self._install_fields(self.fields)

        status = ttk.Label(self, textvariable=self.status_var, padding=(12, 6), anchor="w")
        status.grid(row=3, column=0, sticky="ew")
        self._set_actions_enabled(False)

    def _mount_tree(self) -> None:
        if getattr(self, "tree", None) is not None:
            self.tree.destroy()
        self._columns = self._build_columns()
        names = [field.name for field in self.fields] or [" "]
        self._header_height = max(self._header_font.measure(name) for name in names) + 14
        self.header.configure(height=self._header_height)
        self.tree = ttk.Treeview(
            self._table,
            columns=[column.key for column in self._columns],
            show="",
            selectmode="browse",
        )
        for column in self._columns:
            self.tree.column(
                column.key,
                width=column.width,
                minwidth=column.width,
                stretch=False,
                anchor="center",
            )
        self.tree.tag_configure("odd", background="#F4F7FB")
        self.tree.tag_configure("even", background="#FFFFFF")
        self.tree.tag_configure("modified", background="#FFF3C4")
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.y_scroll.configure(command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.y_scroll.set, xscrollcommand=self._on_tree_xscroll)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-1>", self._on_click, add="+")
        self.tree.bind("<Return>", self._edit_current)
        self.tree.bind("<F2>", self._edit_current)
        self.tree.bind("<MouseWheel>", self._on_wheel)
        self.tree.bind("<Shift-MouseWheel>", self._on_shift_wheel)
        self._draw_headers(self._header_font)

    def _draw_headers(self, font: tkfont.Font) -> None:
        self.header.delete("all")
        x = 0
        bottom = self._header_height - 6
        for column in self._columns:
            width = column.width
            self.header.create_rectangle(
                x, 0, x + width, self._header_height, fill="#E8E8E8", outline="#C8C8C8"
            )
            self.header.create_text(
                x + width / 2,
                bottom,
                text=column.title,
                angle=90,
                anchor="w",
                font=font,
                fill="#222222",
                tags="title",
            )
            x += width
        self.header.configure(scrollregion=(0, 0, x, self._header_height))

    def _xview(self, *args) -> None:
        self.tree.xview(*args)

    def _on_tree_xscroll(self, first: str, last: str) -> None:
        self.x_scroll.set(first, last)
        self.header.xview_moveto(first)

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        self.config(menu=menu)

        file_menu = tk.Menu(menu, tearoff=0)
        file_menu.add_command(label="Открыть SGO...", command=self._open, accelerator="Ctrl+O")
        file_menu.add_command(label="Сохранить SGO...", command=self._save_as, accelerator="Ctrl+S")
        file_menu.add_separator()
        file_menu.add_command(label="Выход", command=self._on_close)
        menu.add_cascade(label="Файл", menu=file_menu)
        self.file_menu = file_menu

        edit_menu = tk.Menu(menu, tearoff=0)
        edit_menu.add_command(label="Отменить", command=self._undo, accelerator="Ctrl+Z")
        edit_menu.add_command(label="Повторить", command=self._redo, accelerator="Ctrl+Y")
        edit_menu.add_separator()
        edit_menu.add_command(label="Отменить все правки", command=self._revert_all)
        menu.add_cascade(label="Правка", menu=edit_menu)
        self.edit_menu = edit_menu

        help_menu = tk.Menu(menu, tearoff=0)
        help_menu.add_command(label="О программе", command=self._about)
        menu.add_cascade(label="Справка", menu=help_menu)

    def _bind_keys(self) -> None:
        self.bind("<Control-o>", lambda _event: self._open())
        self.bind("<Control-O>", lambda _event: self._open())
        self.bind("<Control-s>", lambda _event: self._save_as())
        self.bind("<Control-S>", lambda _event: self._save_as())
        self.bind("<Control-z>", self._undo_event)
        self.bind("<Control-Z>", self._undo_event)
        self.bind("<Control-y>", self._redo_event)
        self.bind("<Control-Y>", self._redo_event)

    def _set_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.save_button.state(["!disabled"] if enabled else ["disabled"])
        self.file_menu.entryconfig("Сохранить SGO...", state=state)
        self._update_edit_menu()

    def _update_edit_menu(self) -> None:
        loaded = self.document is not None
        self.edit_menu.entryconfig("Отменить", state="normal" if self.undo_stack else "disabled")
        self.edit_menu.entryconfig("Повторить", state="normal" if self.redo_stack else "disabled")
        dirty = loaded and self.image != self.baseline
        self.edit_menu.entryconfig("Отменить все правки", state="normal" if dirty else "disabled")
        self.reset_button.state(["!disabled"] if dirty else ["disabled"])

    def _on_click(self, event: tk.Event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if row and column:
            self._remember_cell(row, column)

    def _on_double_click(self, event: tk.Event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if row and column:
            self._begin_edit(row, column)

    def _on_wheel(self, event: tk.Event) -> None:
        self._cancel_editor()
        self.tree.yview_scroll(int(-event.delta / 120), "units")

    def _on_shift_wheel(self, event: tk.Event) -> None:
        self._cancel_editor()
        self.tree.xview_scroll(int(-event.delta / 120), "units")

    def _editing_widget_focused(self) -> bool:
        focus = self.focus_get()
        return isinstance(focus, (tk.Entry, ttk.Combobox))

    def _undo_event(self, event: tk.Event):
        if self._editing_widget_focused():
            return None
        self._undo()
        return "break"

    def _redo_event(self, event: tk.Event):
        if self._editing_widget_focused():
            return None
        self._redo()
        return "break"

    def _edit_current(self, _event: tk.Event | None = None):
        if self._current_record is None or self._current_field is None:
            self.status_var.set("Выберите ячейку.")
            return "break"
        self._begin_edit(str(self._current_record), f"#{self._current_field + 1}")
        return "break"

    def _remember_cell(self, row: str, column: str) -> None:
        record_index = int(row)
        column_index = int(column[1:]) - 1
        spec = self._columns[column_index]
        self._current_record = record_index
        self._current_field = spec.field_index
        field = self.fields[spec.field_index]
        offset = self.profile.table_start + record_index * self.profile.record_size + field.offset
        value = field.format(self._record(record_index))
        access = "только чтение" if not field.editable else "можно изменить"
        self.status_var.set(
            f"Запись {record_index + 1}, «{field.name}», смещение 0x{offset:05X}: "
            f"{value} ({access})"
        )

    def _begin_edit(self, row: str, column: str) -> None:
        if self.document is None:
            return
        column_index = int(column[1:]) - 1
        spec = self._columns[column_index]
        field = self.fields[spec.field_index]
        if not spec.editable:
            self._remember_cell(row, column)
            self.status_var.set(f"«{field.name}» только для чтения.")
            return
        self._cancel_editor()
        self._remember_cell(row, column)
        bbox = self._reveal_cell(row, spec.key)
        if not bbox:
            return
        x, y, width, height = bbox
        current_int = field.read(self._record(int(row)))
        current = field.format(self._record(int(row)))
        self._editor_extra = set()
        if field.kind in {KIND_CHOICE, KIND_ENUM}:
            widget: tk.Widget = ttk.Combobox(
                self.tree,
                state="readonly",
                values=field.choices_for(current_int),
                font=("Consolas", 10),
            )
            widget.set(current)
            place_width = max(width, 72)
            known = set(field.choices) if field.kind == KIND_CHOICE else {item for item, _label in field.labels}
            if current_int not in known:
                self._editor_extra.add(current_int)
        else:
            widget = tk.Entry(
                self.tree,
                justify="center",
                font=("Consolas", 10),
                borderwidth=1,
                relief="solid",
            )
            widget.insert(0, current)
            widget.select_range(0, "end")
            place_width = width
        widget.place(x=x, y=y, width=place_width, height=height)
        widget.focus_set()
        self._editor = widget
        self._editor_record = int(row)
        self._editor_field = spec.field_index
        widget.bind("<Return>", lambda _event: self._commit_editor(advance=1))
        widget.bind("<Tab>", lambda _event: self._commit_editor(advance=1))
        widget.bind("<Shift-Tab>", lambda _event: self._commit_editor(advance=-1))
        widget.bind("<ISO_Left_Tab>", lambda _event: self._commit_editor(advance=-1))
        widget.bind("<Escape>", lambda _event: self._cancel_editor())
        if isinstance(widget, ttk.Combobox):
            widget.bind("<<ComboboxSelected>>", lambda _event: self._commit_editor(advance=0))
        else:
            widget.bind("<FocusOut>", self._on_editor_focus_out)

    def _reveal_cell(self, row: str, column_key: str):
        self.tree.see(row)
        self.tree.update_idletasks()
        total = sum(self.tree.column(column.key, "width") for column in self._columns) or 1
        left_px = self.tree.xview()[0] * total
        column_x = 0
        column_width = 0
        for column in self._columns:
            column_width = self.tree.column(column.key, "width")
            if column.key == column_key:
                break
            column_x += column_width
        view = max(self.tree.winfo_width(), 1)
        if column_x < left_px or column_x + column_width > left_px + view:
            self.tree.xview_moveto(max(0.0, (column_x - 16) / total))
            self.tree.update_idletasks()
        return self.tree.bbox(row, column_key)

    def _on_editor_focus_out(self, event: tk.Event) -> None:
        if self._editor is not event.widget:
            return
        self.after_idle(lambda: self._commit_if_current(event.widget))

    def _commit_if_current(self, widget: tk.Widget) -> None:
        if self._editor is widget:
            self._commit_editor(advance=0)

    def _cancel_editor(self, _event: tk.Event | None = None) -> str:
        entry = self._editor
        self._editor = None
        self._editor_extra = set()
        if entry is not None:
            entry.destroy()
        return "break"

    def _commit_editor(self, advance: int = 0) -> str:
        entry = self._editor
        if entry is None:
            return "break"
        text = entry.get()
        record_index = self._editor_record
        field_index = self._editor_field
        extra = set(self._editor_extra)
        self._cancel_editor()
        field = self.fields[field_index]
        value = parse_field_text(field, text, extra)
        if value is None:
            self.status_var.set(_invalid_message(field))
            return "break"
        self._write_field(record_index, field_index, value, record_history=True)
        if advance:
            self._open_adjacent(record_index, field_index, advance)
        return "break"

    def _open_adjacent(self, record_index: int, field_index: int, step: int) -> None:
        count = len(self.fields)
        cursor = record_index * count + field_index + step
        last = self.profile.record_count * count
        while 0 <= cursor < last:
            record, field = divmod(cursor, count)
            if self.fields[field].editable:
                self._begin_edit(str(record), f"#{field + 1}")
                return
            cursor += step

    def _write_field(
        self, record_index: int, field_index: int, value: bytes, record_history: bool
    ) -> bool:
        field = self.fields[field_index]
        if not field.editable:
            return False
        offset = self.profile.table_start + record_index * self.profile.record_size + field.offset
        old = bytes(self.image[offset : offset + field.size])
        if old == value:
            self._refresh_row(record_index)
            self.status_var.set("Значение не изменилось.")
            return False
        if record_history:
            self.undo_stack.append(Change(record_index, field_index, old, value))
            self.redo_stack.clear()
        self.image[offset : offset + field.size] = value
        sgo.apply_bin_checksum(self.image)
        self._refresh_row(record_index)
        self._refresh_checksums()
        self.status_var.set(
            f"Запись {record_index + 1}, «{field.name}»: {_format_stored(field, old)} → "
            f"{_format_stored(field, value)}. КС bin пересчитана."
        )
        return True

    def _undo(self) -> None:
        if not self.undo_stack:
            return
        change = self.undo_stack.pop()
        wrote = self._write_field(
            change.record_index, change.field_index, change.old, record_history=False
        )
        if not wrote:
            self.undo_stack.append(change)
            return
        self.redo_stack.append(change)
        self._update_edit_menu()
        self.status_var.set("Правка отменена. КС bin пересчитана.")

    def _redo(self) -> None:
        if not self.redo_stack:
            return
        change = self.redo_stack.pop()
        wrote = self._write_field(
            change.record_index, change.field_index, change.new, record_history=False
        )
        if not wrote:
            self.redo_stack.append(change)
            return
        self.undo_stack.append(change)
        self._update_edit_menu()
        self.status_var.set("Правка повторена. КС bin пересчитана.")

    def _revert_all(self) -> None:
        if self.document is None or self.image == self.baseline:
            return
        if not messagebox.askyesno(
            "Отменить правки",
            "Вернуть таблицу и контрольную сумму bin к последнему открытому или сохранённому состоянию?",
            parent=self,
        ):
            return
        self._cancel_editor()
        self.image = bytearray(self.baseline)
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._fill_table()
        self._refresh_checksums()
        self.status_var.set("Правки отменены.")

    def _record(self, index: int) -> bytes:
        start = self.profile.table_start + index * self.profile.record_size
        return bytes(self.image[start : start + self.profile.record_size])

    def _baseline_record(self, index: int) -> bytes:
        start = self.profile.table_start + index * self.profile.record_size
        return bytes(self.baseline[start : start + self.profile.record_size])

    def _fill_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index in range(self.profile.record_count):
            self.tree.insert("", "end", iid=str(index), values=self._row_values(index), tags=(self._row_tag(index),))
        if self.profile.record_count:
            self.tree.selection_set("0")
            self.tree.focus("0")

    def _refresh_row(self, index: int) -> None:
        if not self.tree.exists(str(index)):
            return
        self.tree.item(str(index), values=self._row_values(index), tags=(self._row_tag(index),))

    def _row_values(self, index: int) -> tuple[str, ...]:
        record = self._record(index)
        return tuple(field.format(record) for field in self.fields)

    def _row_tag(self, index: int) -> str:
        if self._record(index) != self._baseline_record(index):
            return "modified"
        return "odd" if index % 2 else "even"

    def _changed_table_bytes(self) -> int:
        if not self.image:
            return 0
        start = self.profile.table_start
        end = self.profile.table_end + 1
        return sum(1 for index in range(start, end) if self.image[index] != self.baseline[index])

    def _refresh_checksums(self) -> None:
        if self.document is None or self.block is None:
            self._set_actions_enabled(False)
            return
        bin_info = sgo.calc_bin_checksum(self.image)
        bin_checksum = bin_info["actual"]
        block_checksum = sgo.calculated_block_checksum(self.image)
        _packed, _bin, _block, file_checksum = sgo.build_edited_sgo(
            self.document, self.block, self.image
        )
        self.bin_cs_var.set(
            "КС bin (смещение 0x0004): "
            + _format_change(bin_checksum, self.original_bin_checksum, 4)
        )
        self.block_cs_var.set(
            "КС блока №1 при сохранении: "
            + _format_change(block_checksum, self.original_block_checksum, 4)
        )
        self.file_cs_var.set(
            "КС файла SGO при сохранении: "
            + _format_change(file_checksum, self.original_file_checksum, 8)
        )
        changed = self._changed_table_bytes()
        if self.image != self.baseline:
            details = f" Изменено байт в таблице: {changed}." if changed else ""
            self.dirty_var.set("Есть несохранённые изменения." + details)
        else:
            self.dirty_var.set("Таблица без изменений.")
        self._update_title()
        self._update_edit_menu()

    def _update_title(self) -> None:
        name = self.path.name if self.path else APP_TITLE
        mark = "*" if self.image != self.baseline else ""
        self.title(f"{APP_TITLE} — {name}{mark}")

    def _set_window_icon(self) -> None:
        icon = _asset_path("app.ico")
        if not icon.is_file():
            return
        try:
            self.iconbitmap(str(icon))
        except tk.TclError:
            return

    def _open(self) -> None:
        if self._editor is not None:
            self._commit_editor(advance=0)
        if not self._confirm_discard():
            return
        if self.path is not None:
            initial = self.path.parent
        elif DEFAULT_SGO.parent.is_dir():
            initial = DEFAULT_SGO.parent
        else:
            initial = Path.cwd()
        selected = filedialog.askopenfilename(
            parent=self,
            title="Открыть SGO",
            initialdir=str(initial),
            filetypes=[("SGO", "*.sgo"), ("Все файлы", "*.*")],
        )
        if selected:
            self.load_file(Path(selected))

    def load_file(self, path: Path) -> None:
        self._cancel_editor()
        try:
            document = sgo.parse_sgo(path)
            profile = match_firmware(sgo.container_header(document.raw))
            if profile is None:
                raise ValueError(
                    "Заголовок SGO не совпадает с поддерживаемыми прошивками.\n"
                    "Контрольные суммы при сравнении не учитываются.\n\n"
                    f"Поддерживаются:\n{supported_firmware_names()}"
                )
            block = sgo.calibration_block(document)
            image = sgo.decode_block(document, block)
            if profile.table_end >= len(image):
                raise ValueError(
                    f"Таблица 0x{profile.table_start:X}–0x{profile.table_end:X} "
                    f"не помещается в блок размером 0x{len(image):X}"
                )
            bin_info = sgo.calc_bin_checksum(image)
            file_checksum = sgo.stored_file_checksum(document.raw)
            file_calculated = sgo.calculated_file_checksum(document.raw)
            block_calculated = sgo.calculated_block_checksum(image)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Ошибка", str(exc), parent=self)
            return

        self.document = document
        self.profile = profile
        self.block = block
        self._install_fields(
            bind_choices(
                profile.fields,
                image,
                profile.table_start,
                profile.record_size,
                profile.record_count,
            )
        )
        self.path = path
        self.image = image
        self.baseline = bytearray(image)
        self.original_bin_checksum = bin_info["actual"]
        self.original_block_checksum = block.checksum
        self.original_file_checksum = file_checksum
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._current_record = None
        self._current_field = None

        self.file_var.set(f"Файл: {path}    {document.identification}")
        self.table_var.set(
            f"Таблица {profile.name}: bin 0x{profile.table_start:06X}–0x{profile.table_end:06X}, "
            f"{profile.record_count} записей по {profile.record_size} байта"
        )
        self.block_var.set(
            f"Блок №{block.index}: память 0x{block.start_address:06X}–0x{block.end_address:06X}, "
            f"{block.size} байт"
        )
        self._fill_table()
        self._refresh_checksums()
        self._set_actions_enabled(True)
        self.status_var.set(
            "Файл загружен. Двойной щелчок по ячейке открывает правку, КС bin считается сразу."
        )

        problems = []
        if bin_info["actual"] != bin_info["calculated"] or bin_info["total_16"] != 0:
            problems.append(
                f"КС bin: в файле 0x{bin_info['actual']:04X}, "
                f"расчёт 0x{bin_info['calculated']:04X}"
            )
        if block.checksum != block_calculated:
            problems.append(
                f"КС блока №1: в файле 0x{block.checksum:04X}, расчёт 0x{block_calculated:04X}"
            )
        if file_checksum != file_calculated:
            problems.append(
                f"КС файла SGO: в файле 0x{file_checksum:08X}, расчёт 0x{file_calculated:08X}"
            )
        if problems:
            messagebox.showwarning(
                "Контрольные суммы",
                "В открытом файле контрольные суммы не сходятся:\n\n"
                + "\n".join(problems)
                + "\n\nПри сохранении они будут пересчитаны.",
                parent=self,
            )

    def _save_as(self) -> bool:
        if self.document is None or self.block is None or self.path is None:
            return False
        if self._editor is not None:
            self._commit_editor(advance=0)
        selected = filedialog.asksaveasfilename(
            parent=self,
            title="Сохранить SGO",
            initialdir=str(self.path.parent),
            initialfile=f"{self.path.stem}_edited.sgo",
            defaultextension=".sgo",
            filetypes=[("SGO", "*.sgo"), ("Все файлы", "*.*")],
        )
        if not selected:
            return False
        target = Path(selected)
        try:
            sgo.apply_bin_checksum(self.image)
            packed, bin_checksum, block_checksum, file_checksum = sgo.build_edited_sgo(
                self.document, self.block, self.image
            )
            _write_bytes_atomic(target, packed)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Ошибка", f"Не удалось сохранить файл:\n{exc}", parent=self)
            return False

        self.baseline = bytearray(self.image)
        self.original_bin_checksum = bin_checksum
        self.original_block_checksum = block_checksum
        self.original_file_checksum = file_checksum
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._fill_table()
        self._refresh_checksums()
        self.status_var.set(f"Сохранено: {target}")
        messagebox.showinfo(
            "Сохранено",
            f"Файл сохранён:\n{target}\n\n"
            f"КС bin: 0x{bin_checksum:04X}\n"
            f"КС блока №1: 0x{block_checksum:04X}\n"
            f"КС файла SGO: 0x{file_checksum:08X}",
            parent=self,
        )
        return True

    def _confirm_discard(self) -> bool:
        if self.document is None or self.image == self.baseline:
            return True
        choice = ask_save_choice(self, "В таблице есть несохранённые изменения.")
        if choice == "cancel":
            return False
        if choice == "save":
            return self._save_as()
        return True

    def _on_close(self) -> None:
        if self._editor is not None:
            self._commit_editor(advance=0)
        if self._confirm_discard():
            self.destroy()

    def _about(self) -> None:
        messagebox.showinfo(
            "О программе",
            f"Навайбкодил М. Калугин в 2026 году.\n\n"
            "Правка прошивок ABS 3AA698109__0042, 3AA614109__0023, 3AA614109__0061, 5N0614109__0023, 5N0614109__0161 и 8U0614109__0406.\n"
            "Колонки у этих версий одни и те же, адрес таблицы свой.\n"
            "Списки значений читаются из таблицы открытого файла.\n\n"
            "Обратите внимание: для того, чтобы ABS принял мотор, необходимо, чтобы выполнялись следующие условия: \n\n"
            "1. Тип трансмиссии.\n"
            "Сверяется с сигналом `MO2_Getr_Code` (mMotor_2, ID 0x288): DSG 2WD = `0x0B`, MT 4WD = `0x0F`\n\n"
            "2. Вариант рынка.\n"
            "Сверяется с сигналом `KO3_Land` (mKombi_3, ID 0x520): → AA если {0,1,4,6,7}, BB если {2,3,5}\n\n"
            "3. Объём двигателя.\n"
            "Сверяется с сигналом `MFN_Hubraum` (mMotor_Flexia_neu, ID 0x580)\n\n"
            "4. Мощность двигателя.\n"
            "Значение из сигнала `MFN_Motorleistung` (mMotor_Flexia_neu, ID 0x580) должно быть в диапазоне, ограниченном минимальным и максимальным значением\n\n",
            parent=self,
        )


def _asset_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "assets" / name


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


def main() -> None:
    app = Editor()
    if DEFAULT_SGO.exists():
        app.load_file(DEFAULT_SGO)
    app.mainloop()


if __name__ == "__main__":
    main()
