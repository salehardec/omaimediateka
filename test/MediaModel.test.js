#!/usr/bin/env node
// Тесты чистой модели медиатеки. Запуск: node test/MediaModel.test.js
"use strict"

const assert = require("assert")
const M = require("../MediaModel.js")

let passed = 0
function test(name, fn) {
  try {
    fn()
    passed++
    console.log("ok   - " + name)
  } catch (e) {
    console.error("FAIL - " + name + "\n     " + (e && e.message))
    process.exitCode = 1
  }
}

const LISTING = [
  "1600000000\t100\t/mnt/DCIM/100APPLE/IMG_0002.HEIC",
  "1600000100\t200\t/mnt/DCIM/100APPLE/IMG_0001.JPG",
  "1600000200\t300\t/mnt/DCIM/100APPLE/IMG_E0001.HEIC",
  "1600000300\t400\t/mnt/DCIM/101APPLE/IMG_0100.MOV",
  "1600000400\t500\t/mnt/DCIM/101APPLE/IMG_0099.MP4",
  "1600000500\t600\t/mnt/DCIM/100APPLE/IMG_0001.AAE",
  "1600000600\t700\t/mnt/DCIM/100APPLE/.hidden.jpg",
  "1600000700\t800\t/mnt/DCIM/100APPLE/__MACOSX/x.jpg",
  "1600000800\t900\t/mnt/DCIM/100APPLE/song.mp3",
  "garbage line",
  ""
].join("\n")

test("parseListing: только медиа, sidecar и мусор отброшены", () => {
  const items = M.parseListing(LISTING)
  assert.strictEqual(items.length, 5)
  assert.strictEqual(items.every((i) => i.kind === "photo" || i.kind === "video"), true)
  assert.strictEqual(items.some((i) => i.name === "IMG_0001.AAE"), false)
  assert.strictEqual(items.some((i) => i.name === "song.mp3"), false)
  assert.strictEqual(items.some((i) => i.name === ".hidden.jpg"), false)
})

test("parseListing: ext/kind/folder/num", () => {
  const items = M.parseListing(LISTING)
  const vid = items.find((i) => i.name === "IMG_0100.MOV")
  assert.strictEqual(vid.kind, "video")
  assert.strictEqual(vid.folder, "101APPLE")
  assert.strictEqual(vid.folderNum, 101)
  assert.strictEqual(vid.num, 100)
  const heic = items.find((i) => i.name === "IMG_0002.HEIC")
  assert.strictEqual(heic.ext, "heic")
  assert.strictEqual(heic.kind, "photo")
  assert.strictEqual(heic.folderNum, 100)
})

test("sortItems: новые первыми по номеру папки и файла", () => {
  const items = M.sortItems(M.parseListing(LISTING), "new")
  assert.strictEqual(items[0].name, "IMG_0100.MOV") // 101APPLE
  assert.strictEqual(items[items.length - 1].name, "IMG_0001.JPG") // 100APPLE, наименьший
  const idxA = items.findIndex((i) => i.name === "IMG_0099.MP4")
  const idxB = items.findIndex((i) => i.name === "IMG_0002.HEIC")
  assert.ok(idxB > idxA, "101APPLE идёт раньше 100APPLE в порядке new")
})

test("sortItems: old — обратный порядок", () => {
  const nw = M.sortItems(M.parseListing(LISTING), "new")
  const od = M.sortItems(M.parseListing(LISTING), "old")
  assert.deepStrictEqual(od.map((i) => i.name), nw.map((i) => i.name).reverse())
})

test("numericKey: видео по дате-времени, IMG_E совпадает с IMG", () => {
  assert.strictEqual(M.numericKey("IMG_1234.HEIC"), 1234)
  assert.strictEqual(M.numericKey("IMG_E1234.HEIC"), 1234)
  assert.strictEqual(M.numericKey("VID_20260101_120000.MOV"), 20260101120000)
  assert.strictEqual(M.numericKey("no-number.png"), 0)
})

test("filterItems: по имени и папке, несколько слов", () => {
  const items = M.parseListing(LISTING)
  assert.strictEqual(M.filterItems(items, "0001").length, 2) // IMG_0001.JPG + IMG_E0001.HEIC
  assert.strictEqual(M.filterItems(items, "101apple").length, 2)
  assert.strictEqual(M.filterItems(items, "mov 101").length, 1)
  assert.strictEqual(M.filterItems(items, "").length, 5)
})

test("thumbPath: детерминированный и по варианту", () => {
  const items = M.parseListing(LISTING)
  const a = M.thumbPath("/cache", items[0], "thumb")
  const b = M.thumbPath("/cache", items[0], "thumb")
  assert.strictEqual(a, b)
  assert.ok(a.startsWith("/cache/thumbs/"))
  assert.ok(a.endsWith(".jpg"))
  const p = M.thumbPath("/cache", items[0], "preview")
  assert.ok(p.startsWith("/cache/previews/"))
  assert.notStrictEqual(a, p)
})

test("thumbPath: mtime/size меняют ключ", () => {
  const a = M.parseListing("1\t2\t/x/IMG_1.JPG")[0]
  const b = M.parseListing("1\t99\t/x/IMG_1.JPG")[0]
  assert.notStrictEqual(M.thumbPath("/c", a, "thumb"), M.thumbPath("/c", b, "thumb"))
})

test("formatSize / formatDuration / formatDate", () => {
  assert.strictEqual(M.formatSize(500), "500 Б")
  assert.strictEqual(M.formatSize(2048), "2 КБ")
  assert.strictEqual(M.formatSize(3 * 1024 * 1024), "3.0 МБ")
  assert.strictEqual(M.formatDuration(65), "1:05")
  assert.strictEqual(M.formatDuration(3661), "1:01:01")
  assert.strictEqual(M.formatDuration(0), "0:00")
  assert.strictEqual(M.formatDate(0), "")
  assert.ok(M.formatDate(1600000000).includes("20"))
})

test("countKind", () => {
  const items = M.parseListing(LISTING)
  assert.strictEqual(M.countKind(items, "photo"), 3)
  assert.strictEqual(M.countKind(items, "video"), 2)
})

test("defaultExportDir", () => {
  assert.strictEqual(M.defaultExportDir("/home/u"), "/home/u/Pictures/iPhone")
  assert.strictEqual(M.defaultExportDir("/home/u/"), "/home/u/Pictures/iPhone")
})

test("sanitizeBaseName", () => {
  assert.strictEqual(M.sanitizeBaseName("IMG_0001.HEIC"), "IMG_0001.HEIC")
  assert.strictEqual(M.sanitizeBaseName("a/b\\c.jpg"), "a_b_c.jpg")
  assert.strictEqual(M.sanitizeBaseName("  .hidden.jpg"), "hidden.jpg")
  assert.strictEqual(M.sanitizeBaseName(""), "file")
})

test("uniqueName: без коллизий — имя не меняется", () => {
  assert.strictEqual(M.uniqueName("IMG_0001.JPG", {}), "IMG_0001.JPG")
  assert.strictEqual(M.uniqueName("IMG_0001.JPG", { "OTHER.JPG": true }), "IMG_0001.JPG")
})

test("uniqueName: суффикс (1), (2) перед расширением", () => {
  assert.strictEqual(M.uniqueName("IMG_0001.JPG", { "IMG_0001.JPG": true }), "IMG_0001 (1).JPG")
  const taken = { "IMG_0001.JPG": true, "IMG_0001 (1).JPG": true, "IMG_0001 (2).JPG": true }
  assert.strictEqual(M.uniqueName("IMG_0001.JPG", taken), "IMG_0001 (3).JPG")
})

test("uniqueName: без расширения и многосегментные имена", () => {
  assert.strictEqual(M.uniqueName("movie", { movie: true }), "movie (1)")
  assert.strictEqual(M.uniqueName("VID_2026.01.01.MOV", { "VID_2026.01.01.MOV": true }), "VID_2026.01.01 (1).MOV")
})

test("uniqueName: опасные имена обезвреживаются", () => {
  assert.strictEqual(M.uniqueName("../etc/passwd", {}), "_etc_passwd")
  assert.strictEqual(M.uniqueName("../etc/passwd", { "_etc_passwd": true }), "_etc_passwd (1)")
})

test("destPath", () => {
  assert.strictEqual(M.destPath("/home/u/Pictures/iPhone", "IMG_1.JPG"), "/home/u/Pictures/iPhone/IMG_1.JPG")
  assert.strictEqual(M.destPath("/home/u/Pictures/iPhone/", "IMG_1.JPG"), "/home/u/Pictures/iPhone/IMG_1.JPG")
})

test("allocateNames: учитывает файлы в папке и коллизии внутри пачки", () => {
  assert.deepStrictEqual(
    M.allocateNames(["a.jpg", "b.jpg"], {}),
    ["a.jpg", "b.jpg"]
  )
  assert.deepStrictEqual(
    M.allocateNames(["a.jpg", "a.jpg", "a.jpg"], { "a.jpg": true }),
    ["a (1).jpg", "a (2).jpg", "a (3).jpg"]
  )
  assert.deepStrictEqual(
    M.allocateNames(["IMG_1.HEIC", "IMG_1.HEIC"], { "IMG_1.HEIC": true, "IMG_1 (1).HEIC": true }),
    ["IMG_1 (2).HEIC", "IMG_1 (3).HEIC"]
  )
  assert.deepStrictEqual(M.allocateNames([], { x: true }), [])
})

test("fileWord: русское склонение", () => {
  assert.strictEqual(M.fileWord(1), "файл")
  assert.strictEqual(M.fileWord(2), "файла")
  assert.strictEqual(M.fileWord(4), "файла")
  assert.strictEqual(M.fileWord(5), "файлов")
  assert.strictEqual(M.fileWord(11), "файлов")
  assert.strictEqual(M.fileWord(21), "файл")
  assert.strictEqual(M.fileWord(22), "файла")
  assert.strictEqual(M.fileWord(0), "файлов")
})

// --- поиск по содержимому --------------------------------------------------

const SEARCH_ITEMS = M.parseListing([
  "1600000000\t100\t/mnt/DCIM/100APPLE/IMG_0100.HEIC",
  "1600000100\t200\t/mnt/DCIM/100APPLE/IMG_0100.MOV",
  "1600000200\t300\t/mnt/DCIM/100APPLE/IMG_0200.JPG",
  "1600000300\t400\t/mnt/DCIM/100APPLE/IMG_0300.HEIC"
].join("\n"))
const SEARCH_ROWS = [
  "/mnt/DCIM/100APPLE/IMG_0100.HEIC\tscene\tРыбалка",
  "/mnt/DCIM/100APPLE/IMG_0100.HEIC\tocr\tрыба",
  "/mnt/DCIM/100APPLE/IMG_0100.MOV\tcaption\ta child holding a fish",
  "/mnt/DCIM/100APPLE/IMG_0200.JPG\tscene\tМоре",
  "/mnt/DCIM/100APPLE/IMG_0300.HEIC\tscene\tПляж",
  "/mnt/DCIM/100APPLE/IMG_0300.HEIC\tplace\tМоре"
].join("\n")

function pathOf(name) { return "/mnt/DCIM/100APPLE/" + name }

function findRow(items, name) {
  return items.find((i) => i.name === name)
}

test("parseQuery: регистр, ё->е, пустой/короткий запрос", () => {
  const q = M.parseQuery("  Рыбалка  Зимняя ")
  assert.deepStrictEqual(q.tokens, ["рыбалка", "зимняя"])
  assert.strictEqual(q.raw, "рыбалка  зимняя")
  assert.strictEqual(q.valid, true)
  const q2 = M.parseQuery("Ёж")
  assert.deepStrictEqual(q2.tokens, ["еж"])
  assert.strictEqual(q2.valid, true)
  assert.strictEqual(M.parseQuery("").valid, false)
  assert.strictEqual(M.parseQuery(" ").valid, false)
  assert.strictEqual(M.parseQuery("я").valid, false)
  assert.strictEqual(M.parseQuery("a").valid, false)
})

test("stemRu: обрезка окончаний, короткие стемы не трогает", () => {
  assert.strictEqual(M.stemRu("Рыбалки"), "рыбалк")
  assert.strictEqual(M.stemRu("Зимняя"), "зимн")
  assert.strictEqual(M.stemRu("зимней"), "зимн")
  assert.strictEqual(M.stemRu("документы"), "документ")
  assert.strictEqual(M.stemRu("Море"), "море") // 3 буквы после отсечения -> не режем
  assert.strictEqual(M.stemRu("рыб"), "рыб")
  assert.strictEqual(M.stemRu("fish"), "fish") // латиница без стемминга
})

test("nameMatches: все токены в имени или папке", () => {
  const it = findRow(SEARCH_ITEMS, "IMG_0100.HEIC")
  assert.strictEqual(M.nameMatches(it, M.parseQuery("img_0100")), true)
  assert.strictEqual(M.nameMatches(it, M.parseQuery("100apple")), true)
  assert.strictEqual(M.nameMatches(it, M.parseQuery("0100 100apple")), true)
  assert.strictEqual(M.nameMatches(it, M.parseQuery("0100 999")), false)
})

test("parseSearchRows / groupSearchRows: пути и источники", () => {
  const rows = M.parseSearchRows(SEARCH_ROWS)
  assert.strictEqual(rows.length, 6)
  assert.deepStrictEqual(rows[0], { path: pathOf("IMG_0100.HEIC"), source: "scene", matched: "Рыбалка" })
  const g = M.groupSearchRows(rows)
  assert.deepStrictEqual(g[pathOf("IMG_0100.HEIC")].sources, ["scene", "ocr"])
  assert.deepStrictEqual(g[pathOf("IMG_0100.HEIC")].matched.scene, ["Рыбалка"])
  assert.strictEqual(g[pathOf("IMG_0200.JPG")].sources.length, 1)
})

test("mergeSearch: пустой запрос возвращает список как есть", () => {
  assert.strictEqual(M.mergeSearch(SEARCH_ITEMS, [], "").length, SEARCH_ITEMS.length)
})

test("mergeSearch: имя и содержимое объединяются, есть бейдж источника", () => {
  const rows = M.parseSearchRows([
    pathOf("IMG_0100.HEIC") + "\tscene\tРыбалка",
    pathOf("IMG_0100.HEIC") + "\tocr\tрыба"
  ].join("\n"))
  const res = M.mergeSearch(SEARCH_ITEMS, rows, "рыбалка")
  assert.strictEqual(res.length, 1)
  assert.strictEqual(res[0].name, "IMG_0100.HEIC")
  assert.deepStrictEqual(res[0].sources, ["scene", "ocr"])
  assert.strictEqual(M.badgeLabel(res[0]), "сцена · текст")
  // совпадение только по имени — без бейджа
  const byName = M.mergeSearch(SEARCH_ITEMS, [], "IMG_0300")
  assert.strictEqual(byName.length, 1)
  assert.deepStrictEqual(byName[0].sources, ["name"])
  assert.strictEqual(M.badgeLabel(byName[0]), "")
})

test("mergeSearch: ранжирование по числу содержательных источников", () => {
  const rows = M.parseSearchRows([
    pathOf("IMG_0200.JPG") + "\tscene\tМоре",
    pathOf("IMG_0300.HEIC") + "\tscene\tПляж",
    pathOf("IMG_0300.HEIC") + "\tplace\tМоре"
  ].join("\n"))
  const res = M.mergeSearch(SEARCH_ITEMS, rows, "море")
  assert.deepStrictEqual(res.map((i) => i.name), ["IMG_0300.HEIC", "IMG_0200.JPG"])
  assert.ok(M.searchScore(res[0]) > M.searchScore(res[1]))
})

test("mergeSearch: Live Photo HEIC+MOV схлопывается в фото", () => {
  const rows = M.parseSearchRows([
    pathOf("IMG_0100.HEIC") + "\tocr\tрыба",
    pathOf("IMG_0100.MOV") + "\tcaption\ta child holding a fish"
  ].join("\n"))
  const res = M.mergeSearch(SEARCH_ITEMS, rows, "fish")
  assert.strictEqual(res.length, 1)
  assert.strictEqual(res[0].name, "IMG_0100.HEIC")
  assert.strictEqual(res[0].kind, "photo")
  assert.deepStrictEqual(res[0].sources, ["ocr", "caption"])
  assert.strictEqual(M.badgeLabel(res[0]), "текст · caption")
})

test("dedupeLivePairs: одинаковый тип не схлопывается, источники переносятся", () => {
  const a = { name: "IMG_1.HEIC", kind: "photo", sources: ["scene"], matched: { scene: ["x"] } }
  const b = { name: "IMG_1.MOV", kind: "video", sources: ["caption"], matched: { caption: ["y"] } }
  const c = { name: "IMG_2.HEIC", kind: "photo", sources: ["name"], matched: {} }
  const d = { name: "IMG_2.HEIC", kind: "photo", sources: ["ocr"], matched: { ocr: ["z"] } }
  const res = M.dedupeLivePairs([a, b, c, d])
  assert.strictEqual(res.length, 3)
  assert.deepStrictEqual(res[0].sources, ["scene", "caption"])
  assert.strictEqual(res[0].kind, "photo")
})

test("contentSources / sourceLabel / badgeLabel: до двух меток и +N", () => {
  const it = { sources: ["name", "people", "place", "scene", "caption"] }
  assert.deepStrictEqual(M.contentSources(it), ["scene", "place", "people", "caption"])
  assert.strictEqual(M.sourceLabel("ocr"), "текст")
  assert.strictEqual(M.badgeLabel(it), "сцена · место +2")
  assert.strictEqual(M.badgeLabel({ sources: ["name"] }), "")
})

console.log("\n" + passed + " passed")
