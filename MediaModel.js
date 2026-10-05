// MediaModel.js — чистая модель медиатеки iPhone.
//
// Без Qt и без файловой системы: на входе — сырой TSV от `oma-mediateka list`
// и параметры кэша, на выходе — готовые к показу элементы. Импортируется в QML
// (`import "MediaModel.js" as Model`) и в node для тестов (см. test/).

var PHOTO_EXTS = {
  jpg: true, jpeg: true, png: true, heic: true, heif: true, webp: true, gif: true
}
var VIDEO_EXTS = { mp4: true, mov: true, m4v: true }

var MONTHS_RU = [
  "января", "февраля", "марта", "апреля", "мая", "июня",
  "июля", "августа", "сентября", "октября", "ноября", "декабря"
]

// --- утилиты ---------------------------------------------------------------

function basename(path) {
  var p = String(path || "")
  var i = p.lastIndexOf("/")
  return i >= 0 ? p.slice(i + 1) : p
}

function dirname(path) {
  var p = String(path || "")
  var i = p.lastIndexOf("/")
  return i > 0 ? p.slice(0, i) : ""
}

function extOf(name) {
  var i = String(name || "").lastIndexOf(".")
  return i >= 0 ? String(name).slice(i + 1).toLowerCase() : ""
}

function kindOf(ext) {
  if (VIDEO_EXTS[ext]) return "video"
  if (PHOTO_EXTS[ext]) return "photo"
  return ""
}

// djb2 (unsigned). Достаточно для имени файла кэша; коллизии не критичны
// (хэш включает mtime+size и различающий суффикс варианта).
function hashKey(str) {
  var s = String(str || "")
  var h = 5381
  for (var i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0
  return h.toString(16)
}

// Числовой суффикс имени: IMG_1234 -> 1234, IMG_E1234 -> 1234,
// VID_20260101_120000 -> 20260101120000. Нет числа -> 0.
function numericKey(name) {
  var s = String(name || "")
  var vid = s.match(/^V(?:ID|ideo)[_-]?(\d{8})[_-]?(\d{6})/i)
  if (vid) return parseInt(vid[1], 10) * 1000000 + parseInt(vid[2], 10)
  var m = s.match(/(\d+)(?!.*\d)/)
  return m ? parseInt(m[1], 10) : 0
}

// Номер папки Apple: 100APPLE -> 100. Иначе 0.
function folderKey(folder) {
  var m = String(folder || "").match(/^(\d+)/)
  return m ? parseInt(m[1], 10) : 0
}

function isSidecar(name) {
  var n = String(name || "")
  if (n.length === 0 || n.charAt(0) === ".") return true
  var ext = extOf(n)
  return ext === "aae" || ext === "thm" || ext === "xmp"
}

// --- парсинг листинга ------------------------------------------------------

// Строка: "<mtime>\t<size>\t<path>". Возвращает массив элементов.
function parseListing(raw) {
  var out = []
  var lines = String(raw || "").split("\n")
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i]
    if (line.length === 0 || line.charCodeAt(0) === 10) continue
    var parts = line.split("\t")
    if (parts.length < 3) continue
    var path = parts.slice(2).join("\t")
    if (path.length === 0) continue
    if (path.indexOf("/__MACOSX/") !== -1) continue
    var name = basename(path)
    if (isSidecar(name)) continue
    var ext = extOf(name)
    var kind = kindOf(ext)
    if (kind === "") continue
    var mtime = parseFloat(parts[0])
    var size = parseInt(parts[1], 10)
    if (!isFinite(mtime)) mtime = 0
    if (!isFinite(size) || size < 0) size = 0
    var folder = basename(dirname(path))
    out.push({
      path: path,
      name: name,
      ext: ext,
      kind: kind,
      mtime: mtime,
      size: size,
      folder: folder,
      folderNum: folderKey(folder),
      num: numericKey(name),
      key: hashKey(path + "|" + mtime + "|" + size)
    })
  }
  return out
}

// --- сортировка ------------------------------------------------------------

function compareChrono(a, b) {
  if (a.folderNum !== b.folderNum) return a.folderNum - b.folderNum
  if (a.num !== b.num) return a.num - b.num
  if (a.mtime !== b.mtime) return a.mtime - b.mtime
  return a.path < b.path ? -1 : a.path > b.path ? 1 : 0
}

// order: "new" (по умолчанию) | "old".
function sortItems(items, order) {
  var copy = (items || []).slice()
  copy.sort(compareChrono)
  if (order !== "old") copy.reverse()
  return copy
}

// --- фильтр ----------------------------------------------------------------

function filterItems(items, query) {
  var q = String(query || "").trim().toLowerCase()
  if (q === "") return (items || []).slice()
  var words = q.split(/\s+/)
  var out = []
  for (var i = 0; i < (items || []).length; i++) {
    var hay = items[i].name.toLowerCase() + " " + items[i].folder.toLowerCase()
    var ok = true
    for (var w = 0; w < words.length; w++) {
      if (hay.indexOf(words[w]) === -1) { ok = false; break }
    }
    if (ok) out.push(items[i])
  }
  return out
}

// --- поиск по содержимому (родной индекс Apple) ----------------------------
//
// Хелпер `oma-mediateka search` возвращает строки TSV `path<TAB>source<TAB>
// matched`. Модель разбирает запрос зеркально Python-части, группирует
// строки по пути, объединяет их с обычным фильтром по имени/папке, схлопывает
// пары Live Photo и ранжирует: чем больше содержательных источников совпало,
// тем выше элемент.

var SOURCE_ORDER = ["name", "scene", "action", "activity", "ocr", "library", "place", "people", "pet", "caption"]
var SOURCE_LABELS = {
  name: "имя",
  scene: "сцена",
  action: "действие",
  activity: "событие",
  ocr: "текст",
  library: "метка",
  place: "место",
  people: "люди",
  pet: "питомец",
  caption: "caption"
}

// Те же общие окончания, что и в bin/oma-mediateka-index.py, отсортированные
// от длинных к коротким. Отсекается одно, стем короче 4 символов не трогаем.
var RU_SUFFIXES = [
  "иями", "ями", "ами", "ией", "иях", "иям", "ием", "ыми", "ими", "ого",
  "его", "ому", "ему", "ых", "их", "ая", "яя", "ое", "ее", "ые", "ие",
  "ой", "ей", "ий", "ый", "ом", "ем", "ам", "ям", "ах", "ях", "ов", "ев",
  "ью", "ия", "ию", "ие", "ей", "а", "я", "о", "е", "у", "ю", "ы", "и", "ь"
]
var RU_SUFFIXES_SORTED = RU_SUFFIXES.slice().sort(function(a, b) { return b.length - a.length })

function normalizeText(s) {
  var t = String(s || "").trim().toLowerCase().replace(/ё/g, "е")
  return t.replace(/^[^0-9a-zа-я]+|[^0-9a-zа-я]+$/g, "")
}

function stemRu(word) {
  var w = normalizeText(word)
  if (w === "" || !/[а-я]/.test(w)) return w
  for (var i = 0; i < RU_SUFFIXES_SORTED.length; i++) {
    var suf = RU_SUFFIXES_SORTED[i]
    if (w.length - suf.length >= 4 && w.slice(-suf.length) === suf)
      return w.slice(0, w.length - suf.length)
  }
  return w
}

// Разбор запроса: нижний регистр, ё→е, токены от 2 символов. valid — можно ли
// вообще идти в индекс (пустой/однобуквенный запрос не ищем).
function parseQuery(query) {
  var raw = String(query || "").trim().toLowerCase().replace(/ё/g, "е")
  var parts = raw.split(/\s+/)
  var tokens = []
  for (var i = 0; i < parts.length; i++) if (parts[i].length >= 2) tokens.push(parts[i])
  var stems = []
  for (var j = 0; j < tokens.length; j++) stems.push(stemRu(tokens[j]))
  return { raw: raw, tokens: tokens, stems: stems, valid: tokens.length > 0 && raw.length >= 2 }
}

function nameMatches(item, parsed) {
  if (!item || !parsed || parsed.tokens.length === 0) return false
  var hay = (String(item.name || "") + " " + String(item.folder || "")).toLowerCase().replace(/ё/g, "е")
  for (var i = 0; i < parsed.tokens.length; i++)
    if (hay.indexOf(parsed.tokens[i]) === -1) return false
  return true
}

function parseSearchRows(raw) {
  var out = []
  var lines = String(raw || "").split("\n")
  for (var i = 0; i < lines.length; i++) {
    if (lines[i] === "") continue
    var parts = lines[i].split("\t")
    if (parts.length < 3 || parts[0] === "") continue
    out.push({ path: parts[0], source: parts[1], matched: parts.slice(2).join("\t") })
  }
  return out
}

// path -> { sources: [...], matched: { source: [text, …] } }
function groupSearchRows(rows) {
  var map = {}
  for (var i = 0; i < (rows || []).length; i++) {
    var r = rows[i]
    var g = map[r.path]
    if (!g) { g = { path: r.path, sources: [], matched: {} }; map[r.path] = g }
    if (g.sources.indexOf(r.source) === -1) g.sources.push(r.source)
    if (!g.matched[r.source]) g.matched[r.source] = []
    if (g.matched[r.source].indexOf(r.matched) === -1) g.matched[r.source].push(r.matched)
  }
  return map
}

function sortSources(list) {
  return (list || []).slice().sort(function(a, b) {
    var ia = SOURCE_ORDER.indexOf(a), ib = SOURCE_ORDER.indexOf(b)
    if (ia < 0) ia = 99
    if (ib < 0) ib = 99
    return ia - ib
  })
}

function sourceLabel(source) { return SOURCE_LABELS[source] || String(source || "") }

function contentSources(item) {
  var out = []
  var list = (item && item.sources) || []
  for (var i = 0; i < list.length; i++) if (list[i] !== "name") out.push(list[i])
  return sortSources(out)
}

// Короткая подпись бейджа: до двух содержательных источников, дальше «+N».
function badgeLabel(item) {
  var cs = contentSources(item)
  if (cs.length === 0) return ""
  var labels = []
  for (var i = 0; i < cs.length && i < 2; i++) labels.push(sourceLabel(cs[i]))
  var s = labels.join(" · ")
  if (cs.length > 2) s += " +" + (cs.length - 2)
  return s
}

// Уникальные совпавшие термины из индекса (без имени файла), максимум limit.
// Нужны, чтобы в просмотрщике было видно, чем именно ассет зацепился за запрос
// (например, меткой действия из MediaAnalysis).
function matchedTerms(item, limit) {
  var out = []
  var matched = (item && item.matched) || {}
  var sources = sortSources(Object.keys(matched))
  for (var i = 0; i < sources.length; i++) {
    if (sources[i] === "name") continue
    var list = matched[sources[i]] || []
    for (var j = 0; j < list.length; j++) {
      var t = String(list[j])
      if (t !== "" && out.indexOf(t) === -1) out.push(t)
    }
  }
  var n = (limit === undefined || limit <= 0) ? 5 : limit
  return out.slice(0, n)
}

function baseNameOf(name) {
  var n = String(name || "")
  var i = n.lastIndexOf(".")
  return i > 0 ? n.slice(0, i) : n
}

function searchScore(item) {
  var s = 0
  var list = (item && item.sources) || []
  for (var i = 0; i < list.length; i++) s += (list[i] === "name") ? 1 : 3
  return s
}

function cloneItem(it) {
  var c = {}
  for (var k in it) c[k] = it[k]
  return c
}

function mergeSources(dst, src) {
  var list = (src && src.sources) || []
  for (var i = 0; i < list.length; i++)
    if (dst.sources.indexOf(list[i]) === -1) dst.sources.push(list[i])
  var matched = (src && src.matched) || {}
  for (var s in matched) {
    if (!dst.matched[s]) dst.matched[s] = []
    for (var j = 0; j < matched[s].length; j++)
      if (dst.matched[s].indexOf(matched[s][j]) === -1) dst.matched[s].push(matched[s][j])
  }
  dst.sources = sortSources(dst.sources)
}

// Live Photo — это HEIC + MOV с одним базовым именем; в выдаче оставляем
// фото и переносим на него источники обоих файлов.
function dedupeLivePairs(items) {
  var byBase = {}
  var out = []
  for (var i = 0; i < (items || []).length; i++) {
    var it = items[i]
    var base = baseNameOf(it.name).toLowerCase()
    var prev = byBase[base]
    if (prev === undefined) { byBase[base] = out.length; out.push(it); continue }
    var kept = out[prev]
    if (kept.kind !== it.kind) {
      var photo = kept.kind === "photo" ? kept : it
      if (photo !== kept) out[prev] = photo
      mergeSources(photo, kept)
      mergeSources(photo, it)
      photo._pos = Math.min(kept._pos || 0, it._pos || 0)
    } else {
      out.push(it)
    }
  }
  return out
}

// Объединяет совпадения по имени/папке с результатами индекса, дедуплицирует
// и ранжирует. items — исходный список (в хронологическом порядке).
function mergeSearch(items, rows, query) {
  var parsed = parseQuery(query)
  if (!parsed.valid) return (items || []).slice()
  var groups = groupSearchRows(rows)
  var out = []
  for (var i = 0; i < (items || []).length; i++) {
    var it = items[i]
    var g = groups[it.path]
    var nameHit = nameMatches(it, parsed)
    if (!g && !nameHit) continue
    var c = cloneItem(it)
    c.sources = g ? g.sources.slice() : []
    c.matched = {}
    if (g) for (var s in g.matched) c.matched[s] = g.matched[s].slice()
    if (nameHit && c.sources.indexOf("name") === -1) c.sources.unshift("name")
    c.sources = sortSources(c.sources)
    c._pos = i
    out.push(c)
  }
  out = dedupeLivePairs(out)
  out.sort(function(a, b) {
    var d = searchScore(b) - searchScore(a)
    if (d !== 0) return d
    return (a._pos || 0) - (b._pos || 0)
  })
  for (var k = 0; k < out.length; k++) delete out[k]._pos
  return out
}

// --- кэш -------------------------------------------------------------------

function thumbFileName(item, variant) {
  return hashKey(item.key + "|" + (variant || "thumb")) + ".jpg"
}

// variant: "thumb" | "preview"
function thumbPath(cacheDir, item, variant) {
  var dir = String(cacheDir || "") + "/" + (variant === "preview" ? "previews" : "thumbs")
  return dir + "/" + thumbFileName(item, variant)
}

// --- форматирование --------------------------------------------------------

function formatSize(bytes) {
  var b = Number(bytes) || 0
  if (b < 1024) return b + " Б"
  var kb = b / 1024
  if (kb < 1024) return Math.round(kb) + " КБ"
  var mb = kb / 1024
  if (mb < 1024) return (mb < 10 ? mb.toFixed(1) : Math.round(mb)) + " МБ"
  var gb = mb / 1024
  return (gb < 10 ? gb.toFixed(1) : Math.round(gb)) + " ГБ"
}

function formatDuration(seconds) {
  var s = Math.max(0, Math.round(Number(seconds) || 0))
  var h = Math.floor(s / 3600)
  var m = Math.floor((s % 3600) / 60)
  var sec = s % 60
  var mm = (m < 10 ? "0" : "") + m
  var ss = (sec < 10 ? "0" : "") + sec
  if (h > 0) return h + ":" + mm + ":" + ss
  return m + ":" + ss
}

function formatDate(epochSec) {
  var s = Number(epochSec) || 0
  if (s <= 0) return ""
  var d = new Date(s * 1000)
  if (isNaN(d.getTime())) return ""
  return d.getDate() + " " + MONTHS_RU[d.getMonth()] + " " + d.getFullYear()
}

function formatTime(epochSec) {
  var s = Number(epochSec) || 0
  if (s <= 0) return ""
  var d = new Date(s * 1000)
  if (isNaN(d.getTime())) return ""
  var h = d.getHours(), m = d.getMinutes()
  return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m
}

// --- агрегаты --------------------------------------------------------------

function countKind(items, kind) {
  var n = 0
  for (var i = 0; i < (items || []).length; i++) if (items[i].kind === kind) n++
  return n
}

// --- экспорт ---------------------------------------------------------------

// Каталог экспорта по умолчанию.
function defaultExportDir(home) {
  var h = String(home || "").replace(/\/+$/, "")
  return h + "/Pictures/iPhone"
}

// Безопасное базовое имя: без путей, без ведущих точек, без управляющих
// символов. Пустое -> "file".
function sanitizeBaseName(name) {
  var n = String(name || "").trim()
  n = n.replace(/[\/\\]/g, "_")
  n = n.replace(/^[.\s]+/, "")
  n = n.replace(/[\u0000-\u001f]/g, "")
  if (n === "") n = "file"
  return n
}

function splitExt(name) {
  var i = String(name).lastIndexOf(".")
  if (i <= 0) return [String(name), ""]
  return [String(name).slice(0, i), String(name).slice(i)]
}

// Уникальное имя в каталоге: если `taken[name]` занято, добавляет
// суффикс " (1)", " (2)", … перед расширением (как файловые менеджеры).
// taken — map имя -> true.
function uniqueName(name, taken) {
  var safe = sanitizeBaseName(name)
  var map = taken || {}
  if (map[safe] !== true) return safe
  var parts = splitExt(safe)
  var base = parts[0], ext = parts[1]
  var i = 1
  while (map[base + " (" + i + ")" + ext] === true) i++
  return base + " (" + i + ")" + ext
}

function destPath(dir, name) {
  var d = String(dir || "").replace(/\/+$/, "")
  return d + "/" + name
}

// Русское склонение слова «файл»: 1 файл, 2-4 файла, 5+ файлов.
function fileWord(n) {
  var v = Math.abs(Number(n) || 0)
  var n10 = v % 10, n100 = v % 100
  if (n10 === 1 && n100 !== 11) return "файл"
  if (n10 >= 2 && n10 <= 4 && (n100 < 10 || n100 >= 20)) return "файла"
  return "файлов"
}

// Уникальные имена для пачки: учитывает и уже лежащее в каталоге (`taken`),
// и коллизии внутри самой пачки. Возвращает массив в порядке входа.
function allocateNames(names, taken) {
  var used = {}
  for (var k in (taken || {})) used[k] = true
  var out = []
  for (var i = 0; i < (names || []).length; i++) {
    var n = uniqueName(names[i], used)
    used[n] = true
    out.push(n)
  }
  return out
}

function findIndexByKey(items, key) {
  for (var i = 0; i < (items || []).length; i++) if (items[i].key === key) return i
  return -1
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    parseListing: parseListing,
    sortItems: sortItems,
    filterItems: filterItems,
    parseQuery: parseQuery,
    stemRu: stemRu,
    normalizeText: normalizeText,
    nameMatches: nameMatches,
    parseSearchRows: parseSearchRows,
    groupSearchRows: groupSearchRows,
    mergeSearch: mergeSearch,
    dedupeLivePairs: dedupeLivePairs,
    searchScore: searchScore,
    contentSources: contentSources,
    badgeLabel: badgeLabel,
    matchedTerms: matchedTerms,
    sourceLabel: sourceLabel,
    sortSources: sortSources,
    baseNameOf: baseNameOf,
    thumbPath: thumbPath,
    thumbFileName: thumbFileName,
    formatSize: formatSize,
    formatDuration: formatDuration,
    formatDate: formatDate,
    formatTime: formatTime,
    countKind: countKind,
    findIndexByKey: findIndexByKey,
    defaultExportDir: defaultExportDir,
    sanitizeBaseName: sanitizeBaseName,
    splitExt: splitExt,
    uniqueName: uniqueName,
    destPath: destPath,
    allocateNames: allocateNames,
    fileWord: fileWord,
    hashKey: hashKey,
    numericKey: numericKey,
    folderKey: folderKey,
    kindOf: kindOf,
    extOf: extOf,
    basename: basename,
    dirname: dirname
  }
}
