"""Проверка формата SGO, контрольных сумм и правки таблицы на 3AA698109__0042."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sgo
from record_layout import bind_choices
from reference_header import REFERENCE_HEADER, match_firmware
from abs_editor import (
    DEFAULT_SGO,
    FIELDS,
    RECORD_COUNT,
    RECORD_SIZE,
    TABLE_END,
    TABLE_START,
    Editor,
    build_grid,
    parse_field_text,
    parse_hex_bytes,
    read_help,
)

SAMPLE = DEFAULT_SGO
EXTRACTED = Path(r"D:\YandexDisk\VAG\ABS\SGO\3AA698109__0042_0x004000.bin")


class SgoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not SAMPLE.exists():
            raise unittest.SkipTest(f"нет файла {SAMPLE}")
        cls.document = sgo.parse_sgo(SAMPLE)
        cls.block = sgo.calibration_block(cls.document)
        cls.image = sgo.decode_block(cls.document, cls.block)

    def test_block_and_table(self) -> None:
        self.assertEqual(self.block.index, 1)
        self.assertEqual(self.block.start_address, 0x4000)
        self.assertEqual(self.block.end_address, 0x7FFFF)
        self.assertEqual(self.block.size, 0x7C000)
        self.assertEqual(self.document.identification, "3AA698109__0042.sgm")
        self.assertEqual((TABLE_END - TABLE_START + 1) % 22, 0)
        self.assertEqual(self.image[TABLE_START], 0x65)
        self.assertEqual(self.image[TABLE_START + 42 * 22], 0xE3)

    def test_extracted_bin_matches_decode(self) -> None:
        if not EXTRACTED.exists():
            self.skipTest("нет извлечённого bin")
        self.assertEqual(EXTRACTED.read_bytes(), bytes(self.image))

    def test_original_checksums_match(self) -> None:
        info = sgo.calc_bin_checksum(self.image)
        self.assertEqual(info["actual"], 0xED4F)
        self.assertEqual(info["calculated"], info["actual"])
        self.assertEqual(info["total_16"], 0)
        self.assertEqual(self.block.checksum, sgo.calculated_block_checksum(self.image))
        self.assertEqual(self.block.checksum, 0x95D3)
        stored = sgo.stored_file_checksum(self.document.raw)
        self.assertEqual(stored, sgo.calculated_file_checksum(self.document.raw))
        self.assertEqual(stored, 0x0754E1D4)

    def test_repack_without_edits_is_identical(self) -> None:
        packed, bin_cs, block_cs, file_cs = sgo.build_edited_sgo(
            self.document, self.block, self.image
        )
        self.assertEqual(packed, self.document.raw)
        self.assertEqual(bin_cs, 0xED4F)
        self.assertEqual(block_cs, 0x95D3)
        self.assertEqual(file_cs, 0x0754E1D4)

    def test_recalculate_all_checksums_repairs_every_block(self) -> None:
        self.assertEqual(sgo.recalculate_all_checksums(self.document.raw), self.document.raw)
        broken = bytearray(self.document.raw)
        for block in self.document.blocks:
            image = sgo.decode_block(self.document, block)
            image[4] ^= 0xFF
            image[5] ^= 0xFF
            encoded = bytes(value ^ 0xFF for value in image)
            broken[block.payload_offset : block.payload_offset + block.size] = encoded
            broken[block.offset + 19] ^= 0xFF
            broken[block.offset + 20] ^= 0xFF
        broken[0x15] ^= 0xFF
        repaired = sgo.recalculate_all_checksums(broken)
        parsed = sgo.parse_sgo(repaired)
        self.assertEqual(sgo.stored_file_checksum(repaired), sgo.calculated_file_checksum(repaired))
        for block in parsed.blocks:
            decoded = sgo.decode_block(parsed, block)
            info = sgo.calc_bin_checksum(decoded)
            self.assertEqual(info["actual"], info["calculated"])
            self.assertEqual(info["total_16"], 0)
            self.assertEqual(block.checksum, sgo.calculated_block_checksum(decoded))
        self.assertEqual(sgo.decode_block(parsed, parsed.blocks[1])[TABLE_START], self.image[TABLE_START])

    def test_edit_recalculates_all_checksums(self) -> None:
        for offset, new_value in (
            (TABLE_START + 1, 0x11),
            (TABLE_START + 1, 0xFF),
            (TABLE_START + 2, 0x80),
            (TABLE_END - 1, 0x5A),
            (TABLE_END, 0x5A),
        ):
            with self.subTest(offset=hex(offset), value=new_value):
                self.assertNotEqual(self.image[offset], new_value)
                edited = bytearray(self.image)
                edited[offset] = new_value
                packed, bin_cs, block_cs, file_cs = sgo.build_edited_sgo(
                    self.document, self.block, edited
                )
                self.assertTrue(packed != self.document.raw)
                reparsed = sgo.parse_sgo(packed)
                block = sgo.calibration_block(reparsed)
                decoded = sgo.decode_block(reparsed, block)
                self.assertEqual(decoded[offset], new_value)
                info = sgo.calc_bin_checksum(decoded)
                self.assertEqual(info["total_16"], 0)
                self.assertEqual(info["actual"], info["calculated"])
                self.assertEqual(info["actual"], bin_cs)
                self.assertEqual(sgo.calculated_block_checksum(decoded), block.checksum)
                self.assertEqual(block.checksum, block_cs)
                self.assertEqual(sgo.calculated_file_checksum(reparsed.raw), file_cs)
                self.assertEqual(sgo.stored_file_checksum(reparsed.raw), file_cs)

                other = self.document.blocks[0]
                self.assertEqual(
                    reparsed.raw[other.payload_offset : other.payload_offset + other.size],
                    self.document.raw[other.payload_offset : other.payload_offset + other.size],
                )
                allowed = set(range(0x15, 0x19))
                allowed.update(
                    (
                        self.block.offset + 19,
                        self.block.offset + 20,
                    )
                )
                allowed.update(
                    range(self.block.payload_offset, self.block.payload_offset + self.block.size)
                )
                diffs = [index for index, (a, b) in enumerate(zip(self.document.raw, packed)) if a != b]
                self.assertTrue(diffs)
                self.assertTrue(set(diffs) <= allowed)
                payload = self.block.payload_offset
                payload_diffs = [index - payload for index in diffs if index in range(payload, payload + self.block.size)]
                self.assertTrue(set(payload_diffs) <= {offset, 4, 5})

    def test_reverting_a_byte_restores_the_file(self) -> None:
        edited = bytearray(self.image)
        edited[TABLE_START + 5] ^= 0x01
        sgo.apply_bin_checksum(edited)
        edited[TABLE_START + 5] ^= 0x01
        sgo.apply_bin_checksum(edited)
        self.assertEqual(edited, self.image)
        packed, *_ = sgo.build_edited_sgo(self.document, self.block, edited)
        self.assertEqual(packed, self.document.raw)

    def test_header_matches_this_version_only(self) -> None:
        other = SAMPLE.with_name("3C0614109__0005.sgo")
        reference = sgo.container_header(self.document.raw)
        self.assertEqual(REFERENCE_HEADER, reference)
        patched = SAMPLE.with_name("3AA698109__0042_PATCHED_FOR_ACC.sgo")
        if patched.exists():
            self.assertEqual(reference, sgo.container_header(patched.read_bytes()))
        with self.assertRaises(ValueError):
            sgo.require_same_header(other.read_bytes(), self.document.raw)
        edited = bytearray(self.image)
        edited[TABLE_START + 4] = 0x0D
        packed, *_rest = sgo.build_edited_sgo(self.document, self.block, edited)
        self.assertEqual(sgo.container_header(packed), reference)

    def test_0023_stock_uses_same_checksums_and_columns(self) -> None:
        path = SAMPLE.with_name("3AA614109__0023.sgo")
        document = sgo.parse_sgo(path)
        block = sgo.calibration_block(document)
        image = sgo.decode_block(document, block)
        info = sgo.calc_bin_checksum(image)
        self.assertEqual(info["actual"], 0x525B)
        self.assertEqual(info["calculated"], info["actual"])
        self.assertEqual(info["total_16"], 0)
        self.assertEqual(block.checksum, 0x6E31)
        self.assertEqual(block.checksum, sgo.calculated_block_checksum(image))
        self.assertEqual(sgo.stored_file_checksum(document.raw), 0x0733852C)
        self.assertEqual(
            sgo.stored_file_checksum(document.raw),
            sgo.calculated_file_checksum(document.raw),
        )
        profile = match_firmware(sgo.container_header(document.raw))
        self.assertIsNotNone(profile)
        self.assertEqual(profile.name, "3AA614109__0023")
        self.assertEqual(profile.table_start, 0x5D0A0)
        self.assertEqual(profile.table_end, 0x5D451)
        self.assertEqual(profile.record_count, 43)
        table = image[profile.table_start : profile.table_end + 1]
        reference = self.image[TABLE_START : TABLE_END + 1]
        self.assertEqual(table, reference)
        packed, *_rest = sgo.build_edited_sgo(document, block, image)
        self.assertEqual(packed, document.raw)
        self.assertIsNone(match_firmware(sgo.container_header(
            (SAMPLE.with_name("3C0614109__0005.sgo")).read_bytes()
        )))

    def test_0061_stock_checksums_and_shorter_table(self) -> None:
        path = SAMPLE.with_name("3AA614109__0061.sgo")
        document = sgo.parse_sgo(path)
        block = sgo.calibration_block(document)
        image = sgo.decode_block(document, block)
        info = sgo.calc_bin_checksum(image)
        self.assertEqual(info["actual"], 0x4A04)
        self.assertEqual(info["calculated"], info["actual"])
        self.assertEqual(info["total_16"], 0)
        self.assertEqual(block.checksum, 0x1C66)
        self.assertEqual(block.checksum, sgo.calculated_block_checksum(image))
        self.assertEqual(sgo.stored_file_checksum(document.raw), 0x077A389A)
        self.assertEqual(
            sgo.stored_file_checksum(document.raw),
            sgo.calculated_file_checksum(document.raw),
        )
        profile = match_firmware(sgo.container_header(document.raw))
        self.assertEqual(profile.name, "3AA614109__0061")
        self.assertEqual(profile.table_start, 0x3ABBC)
        self.assertEqual(profile.table_end, 0x3AD1B)
        self.assertEqual(profile.record_count, 16)
        bound = bind_choices(
            profile.fields, image, profile.table_start, profile.record_size, profile.record_count
        )
        byte7 = next(field for field in bound if field.offset == 7)
        self.assertEqual(byte7.choices, (0x09,))
        self.assertEqual(image[profile.table_start], 0x02)
        self.assertEqual(image[profile.table_end - 21], 0x2F)
        variant = SAMPLE.with_name("3AA614109__0061_VE.sgo")
        if variant.exists():
            self.assertEqual(
                match_firmware(sgo.container_header(variant.read_bytes())).name,
                "3AA614109__0061",
            )
        packed, *_rest = sgo.build_edited_sgo(document, block, image)
        self.assertEqual(packed, document.raw)

    def test_5n0_0023_stock_checksums_and_table(self) -> None:
        path = SAMPLE.with_name("5N0614109__0023.sgo")
        document = sgo.parse_sgo(path)
        block = sgo.calibration_block(document)
        image = sgo.decode_block(document, block)
        info = sgo.calc_bin_checksum(image)
        self.assertEqual(info["actual"], 0xE5DA)
        self.assertEqual(info["calculated"], info["actual"])
        self.assertEqual(info["total_16"], 0)
        self.assertEqual(block.checksum, 0x06E7)
        self.assertEqual(block.checksum, sgo.calculated_block_checksum(image))
        self.assertEqual(sgo.stored_file_checksum(document.raw), 0x075F1A98)
        self.assertEqual(
            sgo.stored_file_checksum(document.raw),
            sgo.calculated_file_checksum(document.raw),
        )
        profile = match_firmware(sgo.container_header(document.raw))
        self.assertEqual(profile.name, "5N0614109__0023")
        self.assertEqual(profile.table_start, 0x3956C)
        self.assertEqual(profile.table_end, 0x39791)
        self.assertEqual(profile.record_count, 25)
        bound = bind_choices(
            profile.fields, image, profile.table_start, profile.record_size, profile.record_count
        )
        by_offset = {field.offset: field for field in bound}
        self.assertIn(0x00, by_offset[2].choices)
        self.assertIn(0xCC, by_offset[8].choices)
        self.assertEqual(by_offset[7].choices, (0x01, 0x05))
        self.assertEqual(image[profile.table_start], 0x05)
        self.assertEqual(image[profile.table_end - 21], 0x4D)
        packed, *_rest = sgo.build_edited_sgo(document, block, image)
        self.assertEqual(packed, document.raw)

    def test_5n0_0161_stock_checksums_and_table(self) -> None:
        path = SAMPLE.with_name("5N0614109__0161.sgo")
        document = sgo.parse_sgo(path)
        block = sgo.calibration_block(document)
        image = sgo.decode_block(document, block)
        info = sgo.calc_bin_checksum(image)
        self.assertEqual(info["actual"], 0xF06D)
        self.assertEqual(info["calculated"], info["actual"])
        self.assertEqual(info["total_16"], 0)
        self.assertEqual(block.checksum, 0xE917)
        self.assertEqual(block.checksum, sgo.calculated_block_checksum(image))
        self.assertEqual(sgo.stored_file_checksum(document.raw), 0x078B47F0)
        self.assertEqual(
            sgo.stored_file_checksum(document.raw),
            sgo.calculated_file_checksum(document.raw),
        )
        profile = match_firmware(sgo.container_header(document.raw))
        self.assertEqual(profile.name, "5N0614109__0161")
        self.assertEqual(profile.table_start, 0x3D934)
        self.assertEqual(profile.table_end, 0x3DB59)
        self.assertEqual(profile.record_count, 25)
        self.assertEqual(image[profile.table_start], 0x6F)
        self.assertEqual(image[profile.table_end - 21], 0xB7)
        variant = SAMPLE.with_name("5N0614109__0161_AXZ_at_8D.sgo")
        if variant.exists():
            self.assertEqual(
                match_firmware(sgo.container_header(variant.read_bytes())).name,
                "5N0614109__0161",
            )
        packed, *_rest = sgo.build_edited_sgo(document, block, image)
        self.assertEqual(packed, document.raw)

    def test_8u0_0406_stock_checksums_and_table(self) -> None:
        path = SAMPLE.with_name("8U0614109E__0406.sgo")
        document = sgo.parse_sgo(path)
        block = sgo.calibration_block(document)
        image = sgo.decode_block(document, block)
        info = sgo.calc_bin_checksum(image)
        self.assertEqual(info["actual"], 0x25C8)
        self.assertEqual(info["calculated"], info["actual"])
        self.assertEqual(info["total_16"], 0)
        self.assertEqual(block.checksum, 0x2E08)
        self.assertEqual(block.checksum, sgo.calculated_block_checksum(image))
        self.assertEqual(sgo.stored_file_checksum(document.raw), 0x079128F6)
        self.assertEqual(
            sgo.stored_file_checksum(document.raw),
            sgo.calculated_file_checksum(document.raw),
        )
        profile = match_firmware(sgo.container_header(document.raw))
        self.assertEqual(profile.name, "8U0614109__0406")
        self.assertEqual(profile.table_start, 0x3D8D0)
        self.assertEqual(profile.table_end, 0x3DB37)
        self.assertEqual(profile.record_count, 28)
        self.assertEqual(image[profile.table_start], 0x00)
        self.assertEqual(image[profile.table_end - 21], 0xC0)
        for name in ("8U0614109E__0406_AXZ_at_C0.sgo", "8U0614109E__0406_mod.sgo"):
            variant = SAMPLE.with_name(name)
            if variant.exists():
                self.assertEqual(
                    match_firmware(sgo.container_header(variant.read_bytes())).name,
                    "8U0614109__0406",
                )
        packed, *_rest = sgo.build_edited_sgo(document, block, image)
        self.assertEqual(packed, document.raw)

    def test_3c0_0017_uses_block_zero_and_20_byte_records(self) -> None:
        path = SAMPLE.with_name("3C0698109__0017.sgo")
        if not path.exists():
            self.skipTest(f"нет файла {path}")
        document = sgo.parse_sgo(path)
        self.assertEqual(len(document.blocks), 1)
        profile = match_firmware(sgo.container_header(document.raw))
        self.assertEqual(profile.name, "3C0698109__0017")
        self.assertEqual(profile.block_index, 0)
        self.assertEqual(profile.record_size, 20)
        self.assertEqual(profile.record_count, 55)
        block = sgo.calibration_block(document, profile.block_index)
        image = sgo.decode_block(document, block)
        info = sgo.calc_bin_checksum(image)
        self.assertEqual(info["actual"], 0xD202)
        self.assertEqual(info["calculated"], info["actual"])
        self.assertEqual(info["total_16"], 0)
        self.assertEqual(block.checksum, 0xD94B)
        self.assertEqual(sgo.stored_file_checksum(document.raw), 0x04EC91BD)
        record = image[profile.table_start : profile.table_start + 20]
        self.assertEqual(record[0], 0x01)
        self.assertEqual(record[6], 0x00)
        self.assertEqual(record[8], 0x00)
        self.assertEqual(record[9], 0x4D)
        self.assertEqual(int.from_bytes(record[10:12], "big"), 75)
        self.assertEqual(int.from_bytes(record[12:14], "big"), 80)
        self.assertEqual(record[14] * 100, 1900)
        self.assertEqual(record[19], 0x00)
        self.assertEqual(sgo.recalculate_all_checksums(document.raw), document.raw)
        packed, *_rest = sgo.build_edited_sgo(document, block, image)
        self.assertEqual(packed, document.raw)

    def test_field_display_and_input(self) -> None:
        record = self.image[TABLE_START : TABLE_START + 22]
        bound = bind_choices(FIELDS, self.image, TABLE_START, RECORD_SIZE, RECORD_COUNT)
        by_name = {field.name: field for field in bound}
        self.assertEqual(by_name["Байт 7"].choices, (0x03, 0x05))
        self.assertEqual(sum(field.size for field in FIELDS), 22)
        self.assertEqual(by_name["Тип трансмиссии"].format(record), "DSG 2WD")
        self.assertEqual(by_name["Номинальная мощность, кВт"].format(record), "90")
        self.assertEqual(by_name["Мощность с CAN min, кВт"].format(record), "75")
        self.assertEqual(by_name["Мощность с CAN max, кВт"].format(record), "100")
        self.assertEqual(by_name["Объём двигателя"].format(record), "1400")
        self.assertEqual(by_name["Масса автомобиля, кг"].format(record), "1421")
        self.assertEqual(by_name["Вариант рынка"].format(record), "BB")
        self.assertEqual(by_name["Байт 9"].format(record), "00")
        self.assertFalse(by_name["Байт 9"].editable)
        self.assertFalse(by_name["Байт 21"].editable)
        volume = by_name["Объём двигателя"]
        self.assertEqual(parse_field_text(volume, "1400"), bytes([0x0E]))
        self.assertEqual(parse_field_text(volume, "3600"), bytes([0x24]))
        self.assertEqual(parse_field_text(volume, "1450"), bytes([0x0F]))
        self.assertEqual(parse_field_text(by_name["Тип трансмиссии"], "Механика 4WD"), bytes([0x02]))
        self.assertEqual(parse_field_text(by_name["Байт 1"], "02"), bytes([0x02]))
        self.assertIsNone(parse_field_text(by_name["Байт 1"], "04"))
        self.assertEqual(
            parse_field_text(by_name["Номинальная мощность, кВт"], "90"),
            bytes([0x00, 0x5A]),
        )

    def test_roundtrip_written_file(self) -> None:
        edited = bytearray(self.image)
        edited[TABLE_START + 8] = 0x42
        packed, *_ = sgo.build_edited_sgo(self.document, self.block, edited)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "out.sgo"
            path.write_bytes(packed)
            loaded = sgo.parse_sgo(path)
            self.assertEqual(loaded.raw, packed)


class ParseHexTests(unittest.TestCase):
    def test_accepts_byte_forms(self) -> None:
        self.assertEqual(parse_hex_bytes("0a", 1), b"\x0a")
        self.assertEqual(parse_hex_bytes("0x0A", 1), b"\x0a")
        self.assertEqual(parse_hex_bytes("$7", 1), b"\x07")
        self.assertEqual(parse_hex_bytes("0C40", 2), b"\x0c\x40")

    def test_rejects_bad_values(self) -> None:
        self.assertIsNone(parse_hex_bytes("", 1))
        self.assertIsNone(parse_hex_bytes("GG", 1))
        self.assertIsNone(parse_hex_bytes("100", 1))
        self.assertEqual(parse_hex_bytes("1 2", 1), b"\x12")


class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not SAMPLE.exists():
            raise unittest.SkipTest(f"нет файла {SAMPLE}")
        cls.app = Editor()
        cls.app.dialogs = False
        cls.app.load_file(SAMPLE)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "app"):
            cls.app.close()

    def tearDown(self) -> None:
        self.app.image = bytearray(self.app.baseline)
        self.app.undo_stack.clear()
        self.app.redo_stack.clear()
        self.app._fill_table()
        self.app._refresh_checksums()

    def cell(self, row: int, column: int) -> str:
        return self.app.table.item(row, column).text()

    def test_table_shows_readonly_coding_byte(self) -> None:
        self.assertEqual(self.app.table.rowCount(), 43)
        self.assertEqual(self.app.table.columnCount(), len(FIELDS))
        self.assertEqual(self.cell(0, 0), "65")
        self.assertEqual(self.cell(0, 1), "00")
        self.assertEqual(self.cell(0, 6), "DSG 2WD")
        self.assertEqual(self.cell(0, 10), "90")
        self.assertEqual(self.cell(0, 11), "75")
        self.assertEqual(self.cell(0, 12), "100")
        self.assertEqual(self.cell(0, 13), "1400")
        self.assertEqual(self.cell(42, 0), "E3")
        self.assertEqual(self.app.table.horizontalHeaderItem(11).text(), "Мощность с CAN min, кВт")
        self.assertEqual(self.app.table.horizontalHeaderItem(12).text(), "Мощность с CAN max, кВт")
        self.assertIn("CRC TRW", self.app.crc_trw.text())
        self.assertIn("без изменений", self.app.crc_trw.text())
        self.assertIn("0xED4F", self.app.crc_trw.text())
        self.assertFalse(self.app._write_field(0, 0, b"\x00", record_history=True))
        self.assertFalse(self.app._write_field(0, 9, b"\x01", record_history=True))
        self.assertFalse(self.app._write_field(0, 17, b"\x01", record_history=True))
        self.assertEqual(self.app.image[TABLE_START], 0x65)

    def test_header_sections_match_columns(self) -> None:
        header = self.app.table.horizontalHeader()
        self.assertEqual(header.count(), self.app.table.columnCount())
        self.assertGreater(header.height(), 100)
        for index in range(self.app.table.columnCount()):
            self.assertEqual(header.sectionSize(index), self.app.table.columnWidth(index))

    def test_volume_edit_rounds(self) -> None:
        self.app.apply_edit(0, 13, "1450")
        self.assertEqual(self.cell(0, 13), "1500")
        self.assertEqual(self.app.image[TABLE_START + 16], 0x0F)
        self.app._undo()
        self.assertEqual(self.cell(0, 13), "1400")

    def test_loads_0023_at_its_table(self) -> None:
        path = SAMPLE.with_name("3AA614109__0023.sgo")
        self.assertTrue(self.app.load_file(path))
        self.assertIn("0x05D0A0", self.app.table_text)
        self.assertIn("3AA614109__0023", self.app.table_text)
        self.assertIn("0x525B", self.app.crc_trw.text())
        self.assertEqual(self.cell(0, 6), "DSG 2WD")
        self.app.load_file(SAMPLE)
        self.assertEqual(self.app.path, SAMPLE)
        self.assertIn("0x03A31C", self.app.table_text)

    def test_loads_0061_shorter_table(self) -> None:
        path = SAMPLE.with_name("3AA614109__0061.sgo")
        self.assertTrue(self.app.load_file(path))
        self.assertEqual(self.app.table.rowCount(), 16)
        self.assertIn("0x03ABBC", self.app.table_text)
        self.assertIn("0x4A04", self.app.crc_trw.text())
        self.assertEqual(self.cell(0, 0), "02")
        self.app.load_file(SAMPLE)
        self.assertEqual(self.app.table.rowCount(), 43)

    def test_loads_5n0_0023_table(self) -> None:
        path = SAMPLE.with_name("5N0614109__0023.sgo")
        self.assertTrue(self.app.load_file(path))
        self.assertEqual(self.app.table.rowCount(), 25)
        self.assertIn("0x03956C", self.app.table_text)
        self.assertIn("0xE5DA", self.app.crc_trw.text())
        self.assertEqual(self.cell(0, 0), "05")
        self.assertIn(0xCC, self.app.fields[8].choices)
        self.app.load_file(SAMPLE)
        self.assertEqual(self.app.table.rowCount(), 43)

    def test_loads_5n0_0161_table(self) -> None:
        path = SAMPLE.with_name("5N0614109__0161.sgo")
        self.assertTrue(self.app.load_file(path))
        self.assertEqual(self.app.table.rowCount(), 25)
        self.assertIn("0x03D934", self.app.table_text)
        self.assertIn("0xF06D", self.app.crc_trw.text())
        self.assertEqual(self.cell(0, 0), "6F")
        self.app.load_file(SAMPLE)
        self.assertEqual(self.app.table.rowCount(), 43)

    def test_loads_8u0_0406_table(self) -> None:
        path = SAMPLE.with_name("8U0614109E__0406.sgo")
        self.assertTrue(self.app.load_file(path))
        self.assertEqual(self.app.table.rowCount(), 28)
        self.assertIn("0x03D8D0", self.app.table_text)
        self.assertIn("8U0614109__0406", self.app.table_text)
        self.assertIn("0x25C8", self.app.crc_trw.text())
        self.assertEqual(self.cell(0, 0), "00")
        self.app.load_file(SAMPLE)
        self.assertEqual(self.app.table.rowCount(), 43)

    def test_loads_3c0_0017_shorter_records(self) -> None:
        path = SAMPLE.with_name("3C0698109__0017.sgo")
        if not path.exists():
            self.skipTest(f"нет файла {path}")
        self.assertTrue(self.app.load_file(path))
        self.assertEqual(self.app.table.rowCount(), 55)
        self.assertEqual(self.app.table.columnCount(), 17)
        self.assertIn("0x077AEC", self.app.table_text)
        self.assertIn("3C0698109__0017", self.app.table_text)
        self.assertIn("0xD202", self.app.crc_trw.text())
        self.assertEqual(self.cell(0, 0), "01")
        self.assertEqual(self.cell(0, 6), "Механика 2WD")
        self.assertEqual(self.cell(0, 8), "00")
        self.assertEqual(self.cell(0, 9), "77")
        self.assertEqual(self.cell(0, 10), "75")
        self.assertEqual(self.cell(0, 12), "1900")
        self.assertEqual(self.cell(0, 16), "00")
        self.app.load_file(SAMPLE)
        self.assertEqual(self.app.table.rowCount(), 43)

    def test_other_firmware_header_is_rejected(self) -> None:
        other = SAMPLE.with_name("3C0614109__0005.sgo")
        patched = SAMPLE
        self.assertTrue(self.app.load_file(patched))
        self.assertEqual(self.app.path, patched)
        self.assertFalse(self.app.load_file(other))
        self.assertTrue(self.app._last_error)
        self.assertEqual(self.app.path, patched)
        self.app.load_file(SAMPLE)
        self.assertEqual(self.app.path, SAMPLE)

    def test_edit_updates_cell_checksum_and_undo(self) -> None:
        self.assertTrue(self.app._write_field(0, 1, b"\x02", record_history=True))
        self.assertEqual(self.cell(0, 1), "02")
        self.assertEqual(self.app.table.item(0, 1).background().color().name(), "#3d4a22")
        info = sgo.calc_bin_checksum(self.app.image)
        self.assertEqual(info["actual"], info["calculated"])
        self.assertEqual(info["total_16"], 0)
        self.assertNotEqual(info["actual"], 0xED4F)
        packed, bin_cs, block_cs, file_cs = sgo.build_edited_sgo(
            self.app.document, self.app.block, self.app.image
        )
        self.assertEqual(bin_cs, info["actual"])
        self.assertEqual(block_cs, sgo.calculated_block_checksum(self.app.image))
        self.assertEqual(file_cs, sgo.calculated_file_checksum(packed))
        self.assertIn(f"0x{bin_cs:04X}", self.app.crc_trw.text())
        self.assertIn(f"0x{block_cs:04X}", self.app.crc_block.text())
        self.assertIn(f"0x{file_cs:08X}", self.app.crc_file.text())
        self.assertIn("0xED4F →", self.app.crc_trw.text())
        self.assertNotEqual(self.app.image, self.app.baseline)

        self.app._undo()
        self.assertEqual(self.app.image, self.app.baseline)
        self.assertEqual(self.cell(0, 1), "00")
        self.assertIn("без изменений", self.app.crc_trw.text())

        self.app._redo()
        self.assertEqual(self.app.image[TABLE_START + 1], 0x02)
        info = sgo.calc_bin_checksum(self.app.image)
        self.assertEqual(info["total_16"], 0)
        self.app._undo()

    def test_header_and_cells_share_column_width(self) -> None:
        header, rows = build_grid(self.app, lambda *_args: None, lambda *_args: None)
        self.assertEqual(len(header.controls), self.app.table.columnCount())
        self.assertEqual(len(rows), self.app.table.rowCount())
        self.assertGreater(header.controls[0].height, 100)
        for index, cell in enumerate(header.controls):
            self.assertEqual(cell.width, rows[0].controls[index].width)
            self.assertEqual(cell.width, self.app.column_widths[index])

    def test_help_is_markdown(self) -> None:
        text = read_help()
        self.assertTrue(text.startswith("# "))
        self.assertIn("CRC TRW", text)
        self.assertIn("MO2_Getr_Code", text)


if __name__ == "__main__":
    unittest.main()
