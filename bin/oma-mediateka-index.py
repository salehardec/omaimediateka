#!/usr/bin/env python3
# oma-mediateka-index — построение и поиск по компактному кэшу родного
# поискового индекса Apple Photos.
#
# Подкоманды (вызываются из bin/oma-mediateka, напрямую не для пользователя):
#   index <mount> <cache_dir> <search_db> <udid>
#       Копирует только для чтения Photos.sqlite и MediaAnalysis.sqlite с
#       iPhone (по gvfs-afc) в <cache_dir>, схлопывает WAL на копиях и
#       собирает компактный sqlite-кэш <search_db>:
#         asset(uuid, path, kind)          — путь относительно точки монтирования
#         token(asset_uuid, lexeme, norm, category, source)
#         caption(asset_uuid, text, lang)
#         meta(key, value)                 — отпечатки баз и UDID
#       Индекс инкрементальный: если -wal/-shm баз не изменились (размер+mtime)
#       и UDID тот же — копирование и пересборка пропускаются.
#   search <mount> <search_db> <query>
#       Ищет по нормализованным русским лексемам (сцены, активности, OCR,
#       документы, места, люди, метаданные), по словам английских caption'ов и
#       по имени файла. Печатает TSV: path<TAB>source<TAB>matched.
#
# На телефоне ничего не меняется: только чтение копий. Без python3 команда
# недоступна, остальные подкоманды хелпера работают как раньше.

import os
import plistlib
import re
import shutil
import sqlite3
import struct
import sys

SCHEMA = """
CREATE TABLE IF NOT EXISTS asset(
  uuid TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS token(
  asset_uuid TEXT NOT NULL,
  lexeme TEXT NOT NULL,
  norm TEXT NOT NULL,
  category INTEGER NOT NULL,
  source TEXT NOT NULL,
  provider TEXT
);
CREATE TABLE IF NOT EXISTS caption(
  asset_uuid TEXT NOT NULL,
  text TEXT NOT NULL,
  lang TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_token_norm ON token(norm);
CREATE INDEX IF NOT EXISTS idx_token_asset ON token(asset_uuid);
CREATE INDEX IF NOT EXISTS idx_caption_asset ON caption(asset_uuid);
"""

PHOTO_EXTS = {"jpg", "jpeg", "png", "heic", "heif", "webp", "gif"}
VIDEO_EXTS = {"mp4", "mov", "m4v"}

# Версия формата компактного кэша; при смене логики нормализации/схемы
# инкрементальный кэш пересобирается с нуля.
INDEX_VERSION = "2"

# Порядок источников в выдаче (чем раньше, тем выше в бейдже).
SOURCE_ORDER = [
    "name", "scene", "action", "activity", "ocr", "library", "place",
    "people", "pet", "caption",
]

# Минимальные уверенности MediaAnalysis: ниже порога метка не попадает в
# индекс, иначе на видеокадрах проскакивает шум (например «Егра» на 0.56).
SCENE_MIN_CONF = 0.3
ACTION_MIN_CONF = 0.3
PET_MIN_CONF = 0.7

# Общие русские словоизменительные окончания. Список отсортирован от длинных
# к коротким; отсекается не больше одного, стем короче 3 символов не трогаем.
RU_SUFFIXES = [
    "иями", "ями", "ами", "ией", "иях", "иям", "ием", "ыми", "ими", "ого",
    "его", "ому", "ему", "ых", "их", "ая", "яя", "ое", "ее", "ые", "ие",
    "ой", "ей", "ий", "ый", "ом", "ем", "ам", "ям", "ах", "ях", "ов", "ев",
    "ью", "ия", "ию", "ие", "ей", "а", "я", "о", "е", "у", "ю", "ы", "и", "ь",
]

CYRILLIC = re.compile(r"[а-я]")


def cat_source(cat):
    """Категория лексемы ZLEOLEXEME.ZCATEGORY -> источник для выдачи."""
    if cat == 4060:
        return "action"  # humanAction/NNN: «Прогулка», «Танец», «Бег»
    if cat == 4090:
        return "activity"
    if cat == 4120:
        return "ocr"
    if cat == 4130:
        return "library"
    if cat == 3000 or cat == 3001:
        return "people"
    if 2000 <= cat < 3000:
        return "place"
    if 4000 <= cat < 4100:
        return "scene"
    if 1000 <= cat < 1100:
        return "library"  # сезон, время суток, будни/выходные
    return None


def normalize(text):
    """Нижний регистр, ё->е, обрезка краевой пунктуации."""
    s = (text or "").strip().lower().replace("ё", "е")
    s = re.sub(r"^[^\w]+|[^\w]+$", "", s, flags=re.UNICODE)
    return s


def stem_ru(word):
    """Грубый русский стеммер: отсекает одно общее окончание."""
    w = normalize(word)
    if not w or not CYRILLIC.search(w):
        return w
    for suf in sorted(RU_SUFFIXES, key=len, reverse=True):
        if len(w) - len(suf) >= 4 and w.endswith(suf):
            return w[: len(w) - len(suf)]
    return w


def parse_query(query):
    """Строка -> (tokens, stems). Токены короче 2 символов отбрасываются."""
    raw = (query or "").strip().lower().replace("ё", "е")
    tokens = [t for t in re.split(r"\s+", raw) if len(t) >= 2]
    return tokens, [stem_ru(t) for t in tokens]


def kind_of(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in VIDEO_EXTS:
        return "video"
    if ext in PHOTO_EXTS:
        return "photo"
    return ""


def fingerprint(path):
    """Отпечаток файла и его WAL/SHM: размер+mtime каждого."""
    parts = []
    for suf in ("", "-wal", "-shm"):
        p = path + suf
        try:
            st = os.stat(p)
        except OSError:
            continue
        parts.append("%s:%d:%d" % (suf, st.st_size, int(st.st_mtime)))
    return "|".join(parts)


def open_ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def parse_action_confidences(blob):
    """Разбор ZACTIONSCONFIDENCE (NSKeyedArchiver) -> {humanAction id: conf}.

    Значение — plist с NSDictionary вида {"<id>": <confidence>}. Ошибки разбора
    не критичны: при неизвестном формате просто не добавляем действия.
    """
    try:
        obj = plistlib.loads(blob)
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    objects = obj.get("$objects")
    top = obj.get("$top")
    if not isinstance(objects, list) or not isinstance(top, dict):
        return {}
    try:
        root = objects[top["root"]]
    except (KeyError, IndexError, TypeError):
        return {}
    if not isinstance(root, dict):
        return {}
    keys = root.get("NS.keys") or []
    values = root.get("NS.objects") or []
    out = {}
    for ref_key, ref_val in zip(keys, values):
        try:
            action_id = str(objects[ref_key])
            confidence = objects[ref_val]
        except (IndexError, TypeError):
            continue
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            out[action_id] = float(confidence)
    return out


def load_taxonomy(con_photos):
    """id-таксономия Photos -> русские метки для MediaAnalysis.

    MediaAnalysis хранит только числовые id (`ZSCENEID`, ключ humanAction),
    а их локализованные названия лежат в `ZLEOLEXEME.ZIDENTIFIER` вида
    `scene/NNN` / `humanAction/NNN`.
    """
    taxonomy = {}
    for ident, content in con_photos.execute(
            "SELECT ZIDENTIFIER, ZCONTENT FROM ZLEOLEXEME "
            "WHERE ZIDENTIFIER LIKE 'scene/%' OR ZIDENTIFIER LIKE 'humanAction/%'"):
        text = (content or "").strip()
        if text:
            taxonomy.setdefault(ident, set()).add(text)
    return taxonomy


def copy_db(src, dst_dir, name):
    """Копирует базу и её -wal/-shm в кэш, схлопывает WAL на копии."""
    made = False
    for suf in ("", "-wal", "-shm"):
        s = src + suf
        d = os.path.join(dst_dir, name) + suf
        if os.path.exists(s):
            shutil.copyfile(s, d)
            made = True
        elif os.path.exists(d):
            os.remove(d)
    if made:
        try:
            con = sqlite3.connect(os.path.join(dst_dir, name))
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
        except sqlite3.Error:
            pass
    return made


def get_meta(con, key):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def cmd_index(mount, cache_dir, db_path, udid):
    photos_src = os.path.join(mount, "PhotoData", "Photos.sqlite")
    media_src = os.path.join(mount, "MediaAnalysis", "MediaAnalysis.sqlite")
    photos_copy = os.path.join(cache_dir, "Photos.sqlite")
    media_copy = os.path.join(cache_dir, "MediaAnalysis.sqlite")

    if not os.path.exists(photos_src):
        sys.stderr.write("нет %s — iPhone не смонтирован или нет доступа\n" % photos_src)
        return 1

    os.makedirs(cache_dir, exist_ok=True)
    reuse = os.environ.get("OMA_INDEX_REUSE") == "1"

    fp_photos = fingerprint(photos_src)
    fp_media = fingerprint(media_src) if os.path.exists(media_src) else ""
    copies_exist = os.path.exists(photos_copy)
    need_copy = not (reuse and copies_exist)

    if not reuse and os.path.exists(db_path):
        try:
            con = sqlite3.connect(db_path)
            fp_ok = (
                get_meta(con, "photos_fp") == fp_photos
                and get_meta(con, "media_fp") == fp_media
                and get_meta(con, "udid") == udid
            )
            ver_ok = get_meta(con, "version") == INDEX_VERSION
            has_assets = con.execute("SELECT COUNT(*) FROM asset").fetchone()[0] > 0
            con.close()
            if fp_ok and ver_ok and has_assets:
                print("Индекс актуален")
                return 0
            if fp_ok and copies_exist:
                # Базы не менялись, устарела лишь схема/логика кэша —
                # пересобираем из уже лежащих копий, не перекачивая 350 МБ.
                need_copy = False
        except sqlite3.Error:
            pass

    if need_copy:
        print("Копирую базы с iPhone…")
        sys.stdout.flush()
        copy_db(photos_src, cache_dir, "Photos.sqlite")
        copy_db(media_src, cache_dir, "MediaAnalysis.sqlite")
        # Отпечаток берём ПОСЛЕ копирования: пока качались 350 МБ, телефон мог
        # обновить WAL, и тогда сохранённый «до» отпечаток заставлял бы копировать
        # заново при каждой проверке.
        fp_photos = fingerprint(photos_src)
        fp_media = fingerprint(media_src) if os.path.exists(media_src) else ""

    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    # Миграция версии 1 -> 2: у token появился provider (leo|ma).
    token_cols = {r[1] for r in con.execute("PRAGMA table_info(token)")}
    if "provider" not in token_cols:
        con.execute("ALTER TABLE token ADD COLUMN provider TEXT")

    prev_udid = get_meta(con, "udid")
    prev_version = get_meta(con, "version")
    if (prev_udid and udid and prev_udid != udid) or prev_version != INDEX_VERSION:
        con.executescript("DELETE FROM asset; DELETE FROM token; DELETE FROM caption;")
    existing = {r[0] for r in con.execute("SELECT uuid FROM asset")}

    p = open_ro(photos_copy)
    assets = {}
    for uuid, directory, filename in p.execute(
            "SELECT ZUUID, ZDIRECTORY, ZFILENAME FROM ZASSET"):
        if not uuid or not filename:
            continue
        rel = (directory.strip("/") + "/" + filename) if directory else filename
        assets[uuid] = rel

    current = set(assets)
    removed = existing - current
    for uuid in removed:
        con.execute("DELETE FROM token WHERE asset_uuid=?", (uuid,))
        con.execute("DELETE FROM caption WHERE asset_uuid=?", (uuid,))
        con.execute("DELETE FROM asset WHERE uuid=?", (uuid,))

    new = [u for u in current if u not in existing]
    asset_rows = [(u, assets[u], kind_of(assets[u])) for u in assets]
    con.executemany(
        "INSERT OR REPLACE INTO asset(uuid, path, kind) VALUES(?, ?, ?)", asset_rows)
    con.commit()

    # --- лексемы: id -> множество (текст, категория) ------------------------
    lexemes = {}
    for lid, cat, content in p.execute(
            "SELECT ZLEXEMEID, ZCATEGORY, ZCONTENT FROM ZLEOLEXEME"):
        text = (content or "").strip()
        if not text or cat_source(cat) is None:
            continue
        lexemes.setdefault(lid, set()).add((text, cat))

    # --- id-таксономия для MediaAnalysis (scene/NNN, humanAction/NNN) --------
    taxonomy = load_taxonomy(p)

    # --- токены только для новых ассетов (инкрементально) -------------------
    tokens = []
    newset = set(new)
    total = len(newset)
    done = 0
    step = max(1, total // 20)
    if total:
        for uuid, blob in p.execute(
                "SELECT ZIDENTIFIER, ZLEXEMEIDSDATA FROM ZLEOITEM WHERE ZITEMTYPE=1"):
            if uuid not in newset or not blob or len(blob) < 8:
                continue
            words = struct.unpack("<%dI" % (len(blob) // 4), blob[: len(blob) // 4 * 4])
            if len(words) < 2 or words[0] != 2:  # неизвестная версия формата
                continue
            seen = set()
            for lid in words[1:]:
                for text, cat in lexemes.get(lid, ()):
                    key = (text, cat)
                    if key in seen:
                        continue
                    seen.add(key)
                    tokens.append((uuid, text, stem_ru(text), cat, cat_source(cat)))
            done += 1
            if done % step == 0 or done == total:
                print("Индексирую: %d/%d" % (done, total))
                sys.stdout.flush()
    p.close()
    if tokens:
        con.executemany(
            "INSERT INTO token(asset_uuid, lexeme, norm, category, source, provider) "
            "VALUES(?, ?, ?, ?, ?, 'leo')", tokens)

    # --- английские caption'ы (дешёво, пересобираем целиком) ----------------
    # Кроме общего caption'а ассета берём и посегментные caption'ы видео —
    # они заметно подробнее и дают слова, которых в общем нет.
    con.execute("DELETE FROM caption")
    if fp_media and os.path.exists(media_copy):
        m = open_ro(media_copy)
        caps = []
        seen_caps = set()
        for table in ("ZIMAGECAPTIONRESULT", "ZVIDEOCAPTIONRESULT"):
            try:
                rows = m.execute(
                    "SELECT s.ZLOCALIDENTIFIER, c.ZCAPTION FROM %s c "
                    "JOIN ZASSET s ON s.Z_PK = c.ZASSET" % table)
            except sqlite3.Error:
                continue
            for loc, text in rows:
                uuid = (loc or "")[:36]
                t = (text or "").strip()
                if uuid in assets and t and (uuid, t) not in seen_caps:
                    seen_caps.add((uuid, t))
                    caps.append((uuid, t, "en"))
        try:
            rows = m.execute(
                "SELECT s.ZLOCALIDENTIFIER, c.ZCAPTION FROM ZVIDEOSEGMENTCAPTIONRESULT c "
                "JOIN ZASSET s ON s.Z_PK = c.ZASSET")
        except sqlite3.Error:
            rows = ()
        for loc, text in rows:
            uuid = (loc or "")[:36]
            t = (text or "").strip()
            if uuid in assets and t and (uuid, t) not in seen_caps:
                seen_caps.add((uuid, t))
                caps.append((uuid, t, "en"))
        m.close()
        con.executemany(
            "INSERT INTO caption(asset_uuid, text, lang) VALUES(?, ?, ?)", caps)

    # --- метки MediaAnalysis (сцены по кадрам, действия, питомцы) -------------
    # Пересобираем целиком (provider='ma'): объём небольшой, зато метки не
    # остаются устаревшими, если MediaAnalysis обновилась, а набор ассетов — нет.
    con.execute("DELETE FROM token WHERE provider='ma'")
    ma_tokens = []
    if fp_media and os.path.exists(media_copy):
        m = open_ro(media_copy)
        ma_assets = {}
        for zid, loc in m.execute("SELECT Z_PK, ZLOCALIDENTIFIER FROM ZASSET"):
            if loc:
                ma_assets[zid] = loc[:36]

        # Сцены: числовые ZSCENEID -> русские метки. Для каждой пары
        # (ассет, сцена) берём максимальную уверенность по всем кадрам.
        scene_max = {}
        for zid, sid, conf in m.execute(
                "SELECT ZASSET, ZSCENEID, ZCONFIDENCE FROM ZCLASSIFICATIONRESULT"):
            uuid = ma_assets.get(zid)
            if not uuid or uuid not in assets:
                continue
            key = (uuid, str(sid))
            c = float(conf or 0)
            if c > scene_max.get(key, -1.0):
                scene_max[key] = c
        seen_rows = set()
        for (uuid, sid), conf in scene_max.items():
            if conf < SCENE_MIN_CONF:
                continue
            for label in taxonomy.get("scene/" + sid, ()):
                row = (uuid, label, stem_ru(label), 4000, "scene")
                if row not in seen_rows:
                    seen_rows.add(row)
                    ma_tokens.append(row)

        # Действия человека: id спрятаны в NSKeyedArchiver-блобе.
        for zid, blob in m.execute(
                "SELECT ZASSET, ZACTIONSCONFIDENCE FROM ZHUMANACTIONCLASSIFICATIONRESULT"):
            uuid = ma_assets.get(zid)
            if not uuid or uuid not in assets or not blob:
                continue
            for aid, conf in parse_action_confidences(blob).items():
                if conf < ACTION_MIN_CONF:
                    continue
                for label in taxonomy.get("humanAction/" + aid, ()):
                    row = (uuid, label, stem_ru(label), 4060, "action")
                    if row not in seen_rows:
                        seen_rows.add(row)
                        ma_tokens.append(row)

        # Питомцы: у детектора нет вида, поэтому одна общая метка.
        pet_seen = set()
        for zid, conf in m.execute("SELECT ZASSET, ZPETSCONFIDENCE FROM ZPETSRESULT"):
            uuid = ma_assets.get(zid)
            if not uuid or uuid not in assets or uuid in pet_seen:
                continue
            if float(conf or 0) < PET_MIN_CONF:
                continue
            pet_seen.add(uuid)
            ma_tokens.append((uuid, "Питомец", stem_ru("Питомец"), 0, "pet"))
        m.close()
        if ma_tokens:
            con.executemany(
                "INSERT INTO token(asset_uuid, lexeme, norm, category, source, provider) "
                "VALUES(?, ?, ?, ?, ?, 'ma')", ma_tokens)

    set_meta(con, "udid", udid)
    set_meta(con, "version", INDEX_VERSION)
    set_meta(con, "photos_fp", fp_photos)
    set_meta(con, "media_fp", fp_media)
    con.commit()
    try:
        con.execute("PRAGMA optimize")
    except sqlite3.Error:
        pass
    con.close()

    print("Готово: %d ассетов, +%d лексем (Leo), +%d меток (MediaAnalysis)"
          % (len(assets), len(tokens), len(ma_tokens)))
    return 0


def _matched_norms(norms, stem):
    """Нормы, совпадающие с запросом по префиксу.

    Прямое направление (запрос — префикс лексемы) даёт typeahead-поведение.
    Обратное (лексема — префикс запроса) нужно для падежей вида
    «зимняя» -> «зима»; оно допускается только для норм от 3 символов, иначе
    короткие обрывки вроде OCR-токена «до» ловят «документ».
    """
    out = []
    for n in norms:
        if n.startswith(stem):
            out.append(n)
        elif len(n) >= 3 and stem.startswith(n):
            out.append(n)
    return out


def cmd_search(mount, db_path, query):
    if not os.path.exists(db_path):
        return 1
    tokens, stems = parse_query(query)
    if not tokens:
        return 0

    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    paths = dict(con.execute("SELECT uuid, path FROM asset"))
    norms = [r[0] for r in con.execute("SELECT DISTINCT norm FROM token") if r[0]]

    # uuid -> {token_index -> set((source, matched))}
    hits = {}
    for ti, stem in enumerate(stems):
        want = _matched_norms(norms, stem)
        for i in range(0, len(want), 500):
            chunk = want[i:i + 500]
            sql = ("SELECT asset_uuid, lexeme, source FROM token WHERE norm IN (%s)"
                   % ",".join("?" * len(chunk)))
            for uuid, lexeme, source in con.execute(sql, chunk):
                hits.setdefault(uuid, {}).setdefault(ti, set()).add((source, lexeme))

    for uuid, text in con.execute("SELECT asset_uuid, text FROM caption"):
        words = re.findall(r"[a-z0-9']+", text.lower())
        if not words:
            continue
        for ti, raw in enumerate(tokens):
            if any(w.startswith(raw) for w in words):
                hits.setdefault(uuid, {}).setdefault(ti, set()).add(("caption", text))

    for uuid, rel in paths.items():
        base = os.path.basename(rel).lower()
        for ti, raw in enumerate(tokens):
            if raw in base:
                hits.setdefault(uuid, {}).setdefault(ti, set()).add(("name", base))

    out = []
    for uuid, per_token in hits.items():
        if len(per_token) < len(tokens):
            continue
        agg = {}
        for pairs in per_token.values():
            for source, text in pairs:
                agg.setdefault(source, set()).add(text)
        rel = paths.get(uuid)
        if rel is None:
            continue
        path = (mount.rstrip("/") + "/" + rel) if mount else rel
        for source in sorted(agg, key=lambda s: SOURCE_ORDER.index(s) if s in SOURCE_ORDER else 99):
            for text in sorted(agg[source])[:4]:
                text = re.sub(r"[\t\r\n]+", " ", text).strip()
                out.append((path, source, text))
    con.close()

    out.sort()
    for path, source, text in out:
        print("%s\t%s\t%s" % (path, source, text))
    return 0


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: oma-mediateka-index.py index|search …\n")
        return 2
    cmd = argv[1]
    if cmd == "index":
        if len(argv) < 6:
            sys.stderr.write("usage: index <mount> <cache_dir> <search_db> <udid>\n")
            return 2
        return cmd_index(argv[2], argv[3], argv[4], argv[5])
    if cmd == "search":
        if len(argv) < 5:
            sys.stderr.write("usage: search <mount> <search_db> <query>\n")
            return 2
        return cmd_search(argv[2], argv[3], argv[4])
    sys.stderr.write("неизвестная подкоманда: %s\n" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
