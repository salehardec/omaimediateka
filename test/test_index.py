#!/usr/bin/env python3
# Тесты сборки и поиска компактного кэша (bin/oma-mediateka-index.py) на
# синтетических базах Photos.sqlite / MediaAnalysis.sqlite. Запуск:
#   python3 test/test_index.py
# Проверяют то, что нельзя проверить node-тестами: разбор BLOB-лексем,
# NSKeyedArchiver-блоба действий, пороги уверенности и миграцию версии кэша.

import contextlib
import importlib.util
import io
import os
import plistlib
import shutil
import sqlite3
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PY = os.path.join(os.path.dirname(HERE), "bin", "oma-mediateka-index.py")

spec = importlib.util.spec_from_file_location("oma_index", INDEX_PY)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

passed = 0
failed = 0


def test(name, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        print("ok   - " + name)
    except Exception as e:  # noqa: BLE001
        failed += 1
        print("FAIL - %s\n     %s" % (name, e))


def assert_eq(actual, expected, what=""):
    if actual != expected:
        raise AssertionError("%s: %r != %r" % (what, actual, expected))


PHOTOS_SCHEMA = """
CREATE TABLE ZASSET(ZUUID TEXT, ZDIRECTORY TEXT, ZFILENAME TEXT);
CREATE TABLE ZLEOLEXEME(ZLEXEMEID INTEGER, ZCATEGORY INTEGER,
                         ZCONTENT TEXT, ZIDENTIFIER TEXT);
CREATE TABLE ZLEOITEM(ZIDENTIFIER TEXT, ZLEXEMEIDSDATA BLOB, ZITEMTYPE INTEGER);
"""

MEDIA_SCHEMA = """
CREATE TABLE ZASSET(Z_PK INTEGER PRIMARY KEY, ZLOCALIDENTIFIER TEXT);
CREATE TABLE ZCLASSIFICATIONRESULT(ZASSET INTEGER, ZSCENEID INTEGER, ZCONFIDENCE FLOAT);
CREATE TABLE ZHUMANACTIONCLASSIFICATIONRESULT(ZASSET INTEGER, ZACTIONSCONFIDENCE BLOB);
CREATE TABLE ZPETSRESULT(ZASSET INTEGER, ZPETSCONFIDENCE FLOAT);
CREATE TABLE ZIMAGECAPTIONRESULT(ZASSET INTEGER, ZCAPTION TEXT);
CREATE TABLE ZVIDEOCAPTIONRESULT(ZASSET INTEGER, ZCAPTION TEXT);
CREATE TABLE ZVIDEOSEGMENTCAPTIONRESULT(ZASSET INTEGER, ZCAPTION TEXT);
"""

A1 = "AAAAAAAA-1111-2222-3333-444444444444"
A2 = "BBBBBBBB-5555-6666-7777-888888888888"


def action_blob(values):
    """NSKeyedArchiver-совместимый блоб {"<id>": <conf>}."""
    keys = [plistlib.UID(2 + 2 * i) for i in range(len(values))]
    vals = [plistlib.UID(3 + 2 * i) for i in range(len(values))]
    objects = ["$null", {"$class": plistlib.UID(0), "NS.keys": keys, "NS.objects": vals}]
    for k, v in values.items():
        objects.append(str(k))
        objects.append(float(v))
    objects[1]["$class"] = plistlib.UID(len(objects))
    objects.append({"$classes": ["NSDictionary", "NSObject"], "$classname": "NSDictionary"})
    return plistlib.dumps({
        "$archiver": "NSKeyedArchiver",
        "$objects": objects,
        "$top": {"root": plistlib.UID(1)},
        "$version": 100000,
    }, fmt=plistlib.FMT_BINARY)


def build_mount(root):
    os.makedirs(os.path.join(root, "PhotoData"))
    os.makedirs(os.path.join(root, "MediaAnalysis"))

    p = sqlite3.connect(os.path.join(root, "PhotoData", "Photos.sqlite"))
    p.executescript(PHOTOS_SCHEMA)
    p.executemany("INSERT INTO ZASSET VALUES(?, ?, ?)", [
        (A1, "DCIM/107APPLE", "IMG_0001.MOV"),
        (A2, "DCIM/107APPLE", "IMG_0002.HEIC"),
    ])
    # id, категория, текст, идентификатор таксономии
    p.executemany("INSERT INTO ZLEOLEXEME VALUES(?, ?, ?, ?)", [
        (10, 4000, "Люди", "scene/881"),
        (11, 4000, "Снег", "scene/1238"),
        (12, 4060, "Прогулка", "humanAction/2"),
        (13, 4060, "Бег", "humanAction/5"),
        (14, 4000, "Море", "scene/999"),
        (15, 4000, "Туман", "scene/777"),
    ])
    p.executemany("INSERT INTO ZLEOITEM VALUES(?, ?, 1)", [
        (A1, struct.pack("<2I", 2, 10)),
        (A2, struct.pack("<2I", 2, 14)),
    ])
    p.commit()
    p.close()

    m = sqlite3.connect(os.path.join(root, "MediaAnalysis", "MediaAnalysis.sqlite"))
    m.executescript(MEDIA_SCHEMA)
    m.executemany("INSERT INTO ZASSET VALUES(?, ?)", [
        (1, A1 + "/L0/001"),
        (2, A2 + "/L0/001"),
    ])
    # сцена выше порога добавляет A1 «Снег»; ниже порога — игнорируется
    m.executemany("INSERT INTO ZCLASSIFICATIONRESULT VALUES(?, ?, ?)", [
        (1, 1238, 0.9),
        (1, 881, 0.05),
        (2, 777, 0.1),
    ])
    m.execute("INSERT INTO ZHUMANACTIONCLASSIFICATIONRESULT VALUES(1, ?)",
              (action_blob({2: 0.8, 5: 0.1}),))
    m.execute("INSERT INTO ZPETSRESULT VALUES(2, 0.95)")
    m.execute("INSERT INTO ZIMAGECAPTIONRESULT VALUES(2, 'a cat on a sofa')")
    m.execute("INSERT INTO ZVIDEOCAPTIONRESULT VALUES(1, 'a walk in the snow')")
    m.execute("INSERT INTO ZVIDEOSEGMENTCAPTIONRESULT VALUES(1, 'a woman wearing sunglasses')")
    m.commit()
    m.close()


def run_case(fn):
    """Готовит синтетический iPhone во временной папке и зовёт fn(mount, cache, db)."""
    tmp = tempfile.mkdtemp(prefix="oma-index-test-")
    try:
        mount = os.path.join(tmp, "mount")
        cache = os.path.join(tmp, "cache")
        db = os.path.join(cache, "search.sqlite")
        build_mount(mount)
        fn(mount, cache, db)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def index(mount, cache, db):
    rc = mod.cmd_index(mount, cache, db, "TEST-UDID")
    assert_eq(rc, 0, "cmd_index rc")


def search_lines(db, mount, query):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.cmd_search(mount, db, query)
    return [l for l in buf.getvalue().splitlines() if l]


def case_scene_and_action(mount, cache, db):
    index(mount, cache, db)
    rows = search_lines(db, mount, "снег")
    assert any("\tscene\tСнег" in r and "IMG_0001.MOV" in r for r in rows), rows
    rows = search_lines(db, mount, "прогулка")
    assert any("\taction\tПрогулка" in r for r in rows), rows
    # сцена ниже порога уверенности не попадает
    assert_eq(search_lines(db, mount, "туман"), [], "низкая уверенность")


def case_pets_and_captions(mount, cache, db):
    index(mount, cache, db)
    assert any("IMG_0002.HEIC" in r and "\tpet\tПитомец" in r
               for r in search_lines(db, mount, "питомец"))
    # сегментный caption видео добавляет слова, которых нет в общем
    assert any("IMG_0001.MOV" in r and "sunglasses" in r
               for r in search_lines(db, mount, "sunglasses"))


def case_version_and_incremental(mount, cache, db):
    index(mount, cache, db)
    con = sqlite3.connect(db)
    assert_eq(con.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0],
              "2", "version")
    counts = dict(con.execute(
        "SELECT source || '/' || IFNULL(provider,''), COUNT(*) FROM token GROUP BY 1"))
    assert counts.get("scene/ma", 0) > 0, counts
    assert counts.get("action/ma", 0) > 0, counts
    assert_eq(counts.get("pet/ma", 0), 1, "pet/ma")
    cols = {r[1] for r in con.execute("PRAGMA table_info(token)")}
    assert "provider" in cols, cols
    con.close()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.cmd_index(mount, cache, db, "TEST-UDID")
    assert "Индекс актуален" in buf.getvalue(), buf.getvalue()


def case_migration_v1(mount, cache, db):
    # Кэш старого формата: token без provider, версия 1.
    os.makedirs(cache, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE asset(uuid TEXT PRIMARY KEY, path TEXT, kind TEXT);
        CREATE TABLE token(asset_uuid TEXT, lexeme TEXT, norm TEXT,
                           category INTEGER, source TEXT);
        CREATE TABLE caption(asset_uuid TEXT, text TEXT, lang TEXT);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES('version', '1');
    """)
    con.commit()
    con.close()
    index(mount, cache, db)
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(token)")}
    assert "provider" in cols, cols
    assert con.execute(
        "SELECT COUNT(*) FROM token WHERE source='scene'").fetchone()[0] > 0
    con.close()


def case_action_parser():
    d = mod.parse_action_confidences(action_blob({2: 0.8, 5: 0.1}))
    assert_eq(d.get("2"), 0.8, "id 2")
    assert_eq(d.get("5"), 0.1, "id 5")
    assert_eq(mod.parse_action_confidences(b"garbage"), {}, "мусор -> {}")


test("parse_action_confidences: разбор NSKeyedArchiver",
     lambda: case_action_parser())
test("MediaAnalysis: сцены и действия с порогом уверенности",
     lambda: run_case(case_scene_and_action))
test("MediaAnalysis: питомцы и посегментные caption'ы",
     lambda: run_case(case_pets_and_captions))
test("cmd_index: версия 2, provider, инкрементальный повтор",
     lambda: run_case(case_version_and_incremental))
test("cmd_index: миграция кэша версии 1 (добавляется provider)",
     lambda: run_case(case_migration_v1))


# --- устойчивость к EIO при копировании через AFC --------------------------

def case_copy_retries():
    tmp = tempfile.mkdtemp(prefix="oma-copy-test-")
    try:
        src = os.path.join(tmp, "src.sqlite")
        with open(src, "wb") as f:
            f.write(b"hello-world")
        dst_dir = os.path.join(tmp, "cache")
        calls = {"n": 0}
        orig = mod._copy_file_once

        def flaky(s, t):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError(5, "Input/output error")
            orig(s, t)

        mod._copy_file_once = flaky
        try:
            made = mod.copy_db(src, dst_dir, "Photos.sqlite", attempts=3)
        finally:
            mod._copy_file_once = orig
        assert_eq(made, True, "made")
        assert_eq(calls["n"], 3, "attempts")
        assert_eq(open(os.path.join(dst_dir, "Photos.sqlite"), "rb").read(),
                  b"hello-world", "content")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_copy_failure_preserves():
    tmp = tempfile.mkdtemp(prefix="oma-copy-test-")
    try:
        src = os.path.join(tmp, "src.sqlite")
        with open(src, "wb") as f:
            f.write(b"new-data")
        dst_dir = os.path.join(tmp, "cache")
        os.makedirs(dst_dir)
        dst = os.path.join(dst_dir, "Photos.sqlite")
        with open(dst, "wb") as f:
            f.write(b"OLD-GOOD")
        orig = mod._copy_file_once

        def boom(s, t):
            raise OSError(5, "Input/output error")

        mod._copy_file_once = boom
        try:
            raised = False
            try:
                mod.copy_db(src, dst_dir, "Photos.sqlite", attempts=2)
            except OSError:
                raised = True
            assert raised, "должно бросить OSError"
        finally:
            mod._copy_file_once = orig
        assert_eq(open(dst, "rb").read(), b"OLD-GOOD", "старая копия цела")
        assert not os.path.exists(dst + ".tmp"), "не осталось .tmp"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_index_io_error(mount, cache, db):
    orig = mod._copy_file_once

    def boom(s, t):
        raise OSError(5, "Input/output error")

    mod._copy_file_once = boom
    try:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = mod.cmd_index(mount, cache, db, "TEST-UDID")
    finally:
        mod._copy_file_once = orig
    assert_eq(rc, mod.INDEX_EXIT_IO, "rc")
    assert "разблокируйте iPhone" in buf.getvalue(), buf.getvalue()
    assert "Traceback" not in buf.getvalue(), buf.getvalue()
    # база не должна появиться: сборка не дошла до неё
    assert not os.path.exists(db), "DB не создана при ошибке копирования"


def case_index_locked(mount, cache, db):
    os.makedirs(cache, exist_ok=True)
    held = mod._acquire_lock(cache, timeout=0)
    assert held is not None, "не удалось взять лок"
    os.environ["OMA_INDEX_LOCK_TIMEOUT"] = "0"
    try:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = mod.cmd_index(mount, cache, db, "TEST-UDID")
    finally:
        held.close()
        os.environ.pop("OMA_INDEX_LOCK_TIMEOUT", None)
    assert_eq(rc, mod.INDEX_EXIT_LOCKED, "rc")
    assert "уже" in buf.getvalue(), buf.getvalue()


test("copy_db: ретраи при EIO и успех с третьей попытки",
     lambda: case_copy_retries())
test("copy_db: ошибка не портит старую копию и не оставляет .tmp",
     lambda: case_copy_failure_preserves())


def case_truncated_detection():
    fp = ":10:5|-wal:0:1"
    assert_eq(mod.fingerprint_size(fp), 10, "size")
    assert_eq(mod.fingerprint_size(""), -1, "empty")
    assert_eq(mod.copy_truncated("/no/such/file", fp), True, "missing")
    tmp = tempfile.mkdtemp(prefix="oma-trunc-test-")
    try:
        p = os.path.join(tmp, "copy")
        with open(p, "wb") as f:
            f.write(b"0123456789")
        assert_eq(mod.copy_truncated(p, fp), False, "full")
        with open(p, "wb") as f:
            f.write(b"01234")
        assert_eq(mod.copy_truncated(p, fp), True, "усечённая")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_truncated_copy_heals(mount, cache, db):
    index(mount, cache, db)
    cp = os.path.join(cache, "Photos.sqlite")
    want = os.path.getsize(os.path.join(mount, "PhotoData", "Photos.sqlite"))
    with open(cp, "wb") as f:  # имитируем обрыв старого EIO
        f.write(b"broken")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.cmd_index(mount, cache, db, "TEST-UDID")
    assert_eq(rc, 0, "rc")
    assert "Копирую базы" in buf.getvalue(), buf.getvalue()
    assert_eq(os.path.getsize(cp), want, "копия восстановлена")


test("copy_truncated: различает полную и оборванную копию",
     lambda: case_truncated_detection())
test("cmd_index: усечённая копия перекачивается (самолечение)",
     lambda: run_case(case_truncated_copy_heals))


def case_malformed_copy_heals(mount, cache, db):
    index(mount, cache, db)
    cp = os.path.join(cache, "Photos.sqlite")
    size = os.path.getsize(cp)
    with open(cp, "wb") as f:  # тот же размер, но мусор
        f.write(b"\x00" * size)
    # заставляем пересборку из копий (как при смене версии)
    con = sqlite3.connect(db)
    con.execute("UPDATE meta SET value='1' WHERE key='version'")
    con.commit()
    con.close()
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = mod.cmd_index(mount, cache, db, "TEST-UDID")
    assert_eq(rc, mod.INDEX_EXIT_IO, "rc")
    assert "повреждены" in buf.getvalue(), buf.getvalue()
    assert not os.path.exists(cp), "битая копия удалена"


test("cmd_index: побитая копия -> понятная ошибка и удаление",
     lambda: run_case(case_malformed_copy_heals))

test("cmd_index: EIO -> код 2 и понятное сообщение вместо traceback",
     lambda: run_case(case_index_io_error))
test("cmd_index: занятый лок -> код 3 без копирования",
     lambda: run_case(case_index_locked))

print("\n%d passed" % passed)
if failed:
    sys.exit(1)
