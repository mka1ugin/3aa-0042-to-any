"""Формат SGO (как в sgotool) и контрольные суммы блока ABS TRW 450.

Контрольная сумма bin-образа повторяет block_checksum_gui.py:
сумма знаковых 16-битных слов big-endian по всему образу, слово по
смещению 0x0004 подбирается так, чтобы сумма по модулю 65536 была нулевой.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

HEADER = b"SGML Object File"
FILE_CHECKSUM_OFFSET = 0x15
FILE_CHECKSUM_LENGTH = 4
HEADER_FIELDS_OFFSET = 0x11
IDENTS_POINTER_OFFSET = 0x19
DATABLOCKS_POINTER_OFFSET = 0x2D

BLOCK_HEADER_SIZE = 25
BLOCK_CHECKSUM_OFFSET = 19
MAX_BLOCKS = 64

BIN_ADDR_START_OFFSET = 0x08
BIN_ADDR_END_OFFSET = 0x0C
BIN_CHECKSUM_OFFSET = 0x0004
BIN_HEADER_MIN_SIZE = BIN_ADDR_END_OFFSET + 4
BIN_MIN_BLOCK_SIZE = 16

CALIBRATION_BLOCK_INDEX = 1
CALIBRATION_START = 0x4000
CALIBRATION_END = 0x7FFFF


@dataclass
class Datablock:
    index: int
    offset: int
    start_address: int
    size: int
    checksum: int

    @property
    def end_address(self) -> int:
        return self.start_address + self.size - 1

    @property
    def payload_offset(self) -> int:
        return self.offset + BLOCK_HEADER_SIZE


@dataclass
class SgoDocument:
    raw: bytes
    identification: str
    blocks: list[Datablock]

    def block(self, index: int) -> Datablock:
        if index < 0 or index >= len(self.blocks):
            raise ValueError(f"В файле нет блока №{index}")
        return self.blocks[index]


def parse_sgo(source: Path | str | bytes) -> SgoDocument:
    if isinstance(source, (Path, str)):
        raw = Path(source).read_bytes()
    else:
        raw = source
    if len(raw) < DATABLOCKS_POINTER_OFFSET + 4:
        raise ValueError(f"Файл слишком маленький: {len(raw)} байт")
    if raw[: len(HEADER)] != HEADER:
        raise ValueError("Некорректный файл: нет заголовка «SGML Object File»")

    idents_offset = _u32_le(raw, IDENTS_POINTER_OFFSET)
    datablocks_offset = _u32_le(raw, DATABLOCKS_POINTER_OFFSET)
    identification = _read_identification(raw, idents_offset)
    blocks = _read_blocks(raw, datablocks_offset)
    return SgoDocument(raw=raw, identification=identification, blocks=blocks)


def container_header(raw: bytes) -> bytes:
    """Заголовок SGO до первого блока данных, без контрольной суммы файла.

    Смещения таблицы констант заданы только для одной версии прошивки,
    поэтому при открытии сравнивается именно эта область.
    """
    document = parse_sgo(raw)
    if not document.blocks:
        raise ValueError("В файле нет блоков данных")
    end = document.blocks[0].offset
    if end < FILE_CHECKSUM_OFFSET + FILE_CHECKSUM_LENGTH or end > len(raw):
        raise ValueError("Некорректная длина заголовка SGO")
    header = bytearray(raw[:end])
    header[FILE_CHECKSUM_OFFSET : FILE_CHECKSUM_OFFSET + FILE_CHECKSUM_LENGTH] = b"\x00" * 4
    return bytes(header)


def require_same_header(raw: bytes, reference: bytes) -> None:
    if container_header(raw) != container_header(reference):
        raise ValueError(
            "Заголовок SGO не совпадает с эталоном 3AA698109__0042.\n"
            "Контрольные суммы при сравнении не учитываются.\n"
            "Смещения таблицы констант действительны только для этой версии прошивки."
        )


def calibration_block(document: SgoDocument, index: int = CALIBRATION_BLOCK_INDEX) -> Datablock:
    block = document.block(index)
    if block.start_address != CALIBRATION_START or block.end_address != CALIBRATION_END:
        raise ValueError(
            f"Блок №{index} занимает "
            f"0x{block.start_address:06X}–0x{block.end_address:06X}, "
            f"ожидается 0x{CALIBRATION_START:06X}–0x{CALIBRATION_END:06X}"
        )
    return block


def decode_block(document: SgoDocument, block: Datablock) -> bytearray:
    payload = document.raw[block.payload_offset : block.payload_offset + block.size]
    if len(payload) != block.size:
        raise ValueError(
            f"Блок №{block.index} выходит за пределы файла "
            f"(нужно {block.size} байт данных)"
        )
    return bytearray(b ^ 0xFF for b in payload)


def stored_file_checksum(raw: bytes) -> int:
    return _u32_le(raw, FILE_CHECKSUM_OFFSET)


def calculated_file_checksum(raw: bytes) -> int:
    total = 0
    for index, value in enumerate(raw):
        if index < FILE_CHECKSUM_OFFSET or index >= FILE_CHECKSUM_OFFSET + FILE_CHECKSUM_LENGTH:
            total += value
    return (total - 1) & 0xFFFFFFFF


def calculated_block_checksum(decoded: bytes) -> int:
    return sum(decoded) & 0xFFFF


def read_bin_addresses(data: bytes) -> tuple[int, int]:
    if len(data) < BIN_HEADER_MIN_SIZE:
        raise ValueError(
            f"Образ блока слишком маленький: {len(data):#x} < {BIN_HEADER_MIN_SIZE:#x}"
        )
    start_addr = struct.unpack_from(">I", data, BIN_ADDR_START_OFFSET)[0]
    end_addr = struct.unpack_from(">I", data, BIN_ADDR_END_OFFSET)[0]
    return start_addr, end_addr


def validate_bin_length(start_addr: int, end_addr: int, actual_size: int) -> int:
    block_len = end_addr - start_addr
    if block_len < 0:
        raise ValueError(
            "Некорректная длина блока: адрес конца меньше адреса начала.\n"
            f"  Начало: 0x{start_addr:08X}\n"
            f"  Конец:  0x{end_addr:08X}"
        )
    if block_len < BIN_MIN_BLOCK_SIZE:
        raise ValueError(
            f"Некорректная длина блока: {block_len} байт (минимум {BIN_MIN_BLOCK_SIZE}).\n"
            f"  Начало: 0x{start_addr:08X}\n"
            f"  Конец:  0x{end_addr:08X}"
        )
    if block_len % 2:
        raise ValueError(
            f"Длина блока {block_len} нечётная, сумму 16-битных слов посчитать нельзя"
        )
    if actual_size < block_len:
        raise ValueError(
            "Образ короче длины по адресам.\n"
            f"  Расчётная длина: {block_len} байт (0x{block_len:X})\n"
            f"  Фактический размер: {actual_size} байт (0x{actual_size:X})"
        )
    return block_len


def calc_bin_checksum(data: bytes) -> dict[str, int]:
    """Расчёт из block_checksum_gui.py: calculated записывается на место слова 0x0004."""
    start_addr, end_addr = read_bin_addresses(data)
    end_off = validate_bin_length(start_addr, end_addr, len(data))

    total = 0
    partial = 0
    for off in range(0, end_off, 2):
        word = struct.unpack_from(">h", data, off)[0]
        total += word
        if off != BIN_CHECKSUM_OFFSET:
            partial += word

    return {
        "total_16": total & 0xFFFF,
        "actual": struct.unpack_from(">H", data, BIN_CHECKSUM_OFFSET)[0],
        "calculated": (-partial) & 0xFFFF,
        "start_addr": start_addr,
        "end_addr": end_addr,
    }


def apply_bin_checksum(data: bytearray) -> int:
    calculated = calc_bin_checksum(data)["calculated"]
    struct.pack_into(">H", data, BIN_CHECKSUM_OFFSET, calculated)
    return calculated


def is_bin_image(data: bytes) -> bool:
    try:
        calc_bin_checksum(data)
    except ValueError:
        return False
    return True


def recalculate_all_checksums(raw: bytes, replacements: dict[int, bytes] | None = None) -> bytes:
    """Пересчитать КС bin, КС каждого блока SGO и КС файла.

    replacements — уже расшифрованные образы отдельных блоков (например, правленая
    таблица блока №1). У образа с заголовком BIN сначала чинится слово по смещению
    0x0004, затем сумма блока и сумма всего контейнера.
    """
    document = parse_sgo(raw)
    packed = bytearray(raw)
    overrides = replacements or {}
    for block in document.blocks:
        if block.index in overrides:
            image = bytearray(overrides[block.index])
            if len(image) != block.size:
                raise ValueError(
                    f"Размер образа {len(image)} не равен длине блока №{block.index} ({block.size})"
                )
        else:
            image = decode_block(document, block)
        if is_bin_image(image):
            apply_bin_checksum(image)
        encoded = bytes(value ^ 0xFF for value in image)
        packed[block.payload_offset : block.payload_offset + block.size] = encoded
        block_checksum = calculated_block_checksum(image)
        packed[block.offset + BLOCK_CHECKSUM_OFFSET] = (block_checksum >> 8) & 0xFF
        packed[block.offset + BLOCK_CHECKSUM_OFFSET + 1] = block_checksum & 0xFF
    struct.pack_into("<I", packed, FILE_CHECKSUM_OFFSET, calculated_file_checksum(packed))
    return bytes(packed)


def build_edited_sgo(
    document: SgoDocument, block: Datablock, decoded: bytes
) -> tuple[bytes, int, int, int]:
    """Упаковать образ блока обратно в SGO и пересчитать все три суммы.

    Возвращает (файл, КС bin, КС блока, КС файла).
    Внутренняя КС bin пересчитывается до сумм контейнера: она входит в данные блока.
    """
    if len(decoded) != block.size:
        raise ValueError(
            f"Размер образа {len(decoded)} не равен длине блока №{block.index} ({block.size})"
        )
    image = bytearray(decoded)
    bin_checksum = apply_bin_checksum(image)
    packed = bytearray(document.raw)
    encoded = bytes(value ^ 0xFF for value in image)
    start = block.payload_offset
    packed[start : start + len(encoded)] = encoded
    block_checksum = calculated_block_checksum(image)
    packed[block.offset + BLOCK_CHECKSUM_OFFSET] = (block_checksum >> 8) & 0xFF
    packed[block.offset + BLOCK_CHECKSUM_OFFSET + 1] = block_checksum & 0xFF
    file_checksum = calculated_file_checksum(packed)
    struct.pack_into("<I", packed, FILE_CHECKSUM_OFFSET, file_checksum)
    return bytes(packed), bin_checksum, block_checksum, file_checksum


def _u32_le(raw: bytes, offset: int) -> int:
    return struct.unpack_from("<I", raw, offset)[0]


def _read_identification(raw: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(raw):
        raise ValueError(f"Смещение идентификатора 0x{offset:X} вне файла")
    chars = bytearray()
    index = offset
    while index < len(raw) and raw[index] != 0:
        chars.append(raw[index] ^ 0xFF)
        index += 1
        if len(chars) > 4096:
            raise ValueError("Слишком длинный идентификатор файла")
    if index >= len(raw):
        raise ValueError("Идентификатор файла не завершается нулём")
    return chars.decode("latin-1")


def _read_blocks(raw: bytes, offset: int) -> list[Datablock]:
    if offset < 0 or offset + 4 > len(raw):
        raise ValueError(f"Смещение оглавления блоков 0x{offset:X} вне файла")
    count = _u32_le(raw, offset)
    if not 1 <= count <= MAX_BLOCKS:
        raise ValueError(f"Некорректное число блоков данных: {count}")
    table = offset + 4
    if table + count * 4 > len(raw):
        raise ValueError("Оглавление блоков выходит за пределы файла")
    pointers = [_u32_le(raw, table + index * 4) for index in range(count)]
    return [_read_block(raw, index, pointer) for index, pointer in enumerate(pointers)]


def _read_block(raw: bytes, index: int, offset: int) -> Datablock:
    if offset < 0 or offset + BLOCK_HEADER_SIZE > len(raw):
        raise ValueError(f"Заголовок блока №{index} (0x{offset:X}) вне файла")
    start_address = (raw[offset] << 16) | (raw[offset + 1] << 8) | raw[offset + 2]
    size = (raw[offset + 4] << 16) | (raw[offset + 5] << 8) | raw[offset + 6]
    checksum = (raw[offset + BLOCK_CHECKSUM_OFFSET] << 8) | raw[offset + BLOCK_CHECKSUM_OFFSET + 1]
    if offset + BLOCK_HEADER_SIZE + size > len(raw):
        raise ValueError(
            f"Данные блока №{index} выходят за пределы файла "
            f"(размер 0x{size:X} по смещению 0x{offset:X})"
        )
    return Datablock(
        index=index,
        offset=offset,
        start_address=start_address,
        size=size,
        checksum=checksum,
    )
