"""Раскладка одной записи таблицы констант.

У каждой прошивки свой набор полей. Четыре уже подключённые версии
используют одну и ту же запись из 22 байт. Списки выбора сюда не
зашиты: их читают из таблицы открытого файла.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

KIND_HEX = "hex"
KIND_CHOICE = "choice"
KIND_ENUM = "enum"
KIND_UINT = "uint"
KIND_VOLUME = "volume"

TRANSMISSION = (
    (0x00, "Механика 2WD"),
    (0x10, "Автомат 2WD"),
    (0x40, "DSG 2WD"),
    (0x02, "Механика 4WD"),
    (0x12, "Автомат 4WD"),
    (0x42, "DSG 4WD"),
)


@dataclass(frozen=True)
class Field:
    name: str
    offset: int
    editable: bool
    size: int = 1
    kind: str = KIND_HEX
    choices: tuple[int, ...] = ()
    labels: tuple[tuple[int, str], ...] = ()

    def read(self, record: bytes) -> int:
        return int.from_bytes(record[self.offset : self.offset + self.size], "big")

    def format(self, record: bytes) -> str:
        value = self.read(record)
        if self.kind == KIND_ENUM:
            return dict(self.labels).get(value, f"{value:0{self.size * 2}X}")
        if self.kind == KIND_VOLUME:
            return str(value * 100)
        if self.kind == KIND_UINT:
            return str(value)
        return f"{value:0{self.size * 2}X}"

    def choices_for(self, current: int) -> tuple[str, ...]:
        if self.kind == KIND_ENUM:
            labels = [label for _value, label in self.labels]
            current_label = dict(self.labels).get(current, f"{current:0{self.size * 2}X}")
        else:
            labels = [f"{item:0{self.size * 2}X}" for item in self.choices]
            current_label = f"{current:0{self.size * 2}X}"
        if current_label not in labels:
            labels.append(current_label)
        return tuple(labels)


def validate_fields(fields: tuple[Field, ...]) -> int:
    cursor = 0
    for field in fields:
        if field.offset != cursor or field.size < 1:
            raise ValueError(f"Поле «{field.name}» нарушает раскладку записи")
        cursor += field.size
    if cursor <= 0:
        raise ValueError("В записи нет полей")
    return cursor


def layout_key(fields: tuple[Field, ...]) -> tuple:
    return tuple((field.name, field.offset, field.size, field.kind, field.editable) for field in fields)


def bind_choices(
    fields: tuple[Field, ...],
    image: bytes,
    table_start: int,
    record_size: int,
    record_count: int,
) -> tuple[Field, ...]:
    """Подставить в списки те значения, которые есть в таблице этой прошивки."""
    bound = []
    for field in fields:
        if field.kind not in {KIND_CHOICE, KIND_ENUM} or not field.editable:
            bound.append(field)
            continue
        found: list[int] = []
        for index in range(record_count):
            offset = table_start + index * record_size + field.offset
            value = int.from_bytes(image[offset : offset + field.size], "big")
            if value not in found:
                found.append(value)
        found.sort()
        if field.kind == KIND_ENUM:
            known = dict(field.labels)
            width = field.size * 2
            labels = tuple((value, known.get(value, f"{value:0{width}X}")) for value in found)
            bound.append(replace(field, choices=tuple(found), labels=labels))
        else:
            bound.append(replace(field, choices=tuple(found)))
    return tuple(bound)


# Общая запись четырёх подключённых прошивок. Другая версия может задать свой набор.
CODING_FIELDS: tuple[Field, ...] = (
    Field("2й байт кодировки", 0, False),
    Field("Байт 1", 1, True, kind=KIND_CHOICE),
    Field("Байт 2", 2, True, kind=KIND_CHOICE),
    Field("Байт 3", 3, True, kind=KIND_CHOICE),
    Field("Тормоза 1", 4, True, kind=KIND_CHOICE),
    Field("Тормоза 2", 5, True, kind=KIND_CHOICE),
    Field("Тип трансмиссии", 6, True, kind=KIND_ENUM, labels=TRANSMISSION),
    Field("Байт 7", 7, True, kind=KIND_CHOICE),
    Field("Вариант рынка", 8, True, kind=KIND_CHOICE),
    Field("Байт 9", 9, False),
    Field("Номинальная мощность, кВт", 10, True, size=2, kind=KIND_UINT),
    Field("Минимальная мощность по CAN, кВт", 12, True, size=2, kind=KIND_UINT),
    Field("Максимальная мощность по CAN, кВт", 14, True, size=2, kind=KIND_UINT),
    Field("Объём двигателя", 16, True, kind=KIND_VOLUME),
    Field("Байт 17", 17, True, kind=KIND_CHOICE),
    Field("Масса автомобиля, кг", 18, True, size=2, kind=KIND_UINT),
    Field("Пороги вмешательства ESP", 20, True, kind=KIND_CHOICE),
    Field("Байт 21", 21, False),
)

validate_fields(CODING_FIELDS)
