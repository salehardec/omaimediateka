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

function findIndexByKey(items, key) {
  for (var i = 0; i < (items || []).length; i++) if (items[i].key === key) return i
  return -1
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    parseListing: parseListing,
    sortItems: sortItems,
    filterItems: filterItems,
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
    hashKey: hashKey,
    numericKey: numericKey,
    folderKey: folderKey,
    kindOf: kindOf,
    extOf: extOf,
    basename: basename,
    dirname: dirname
  }
}
