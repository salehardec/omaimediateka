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

console.log("\n" + passed + " passed")
