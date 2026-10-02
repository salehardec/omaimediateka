import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "MediaModel.js" as Model

// Медиатека iPhone по USB. Кнопка в баре -> попап по центру экрана:
// сетка превью из DCIM, фильтр, полноэкранный просмотр выбранного.
// Только просмотр: без импорта, удаления и прочих файлов.
Panel {
  id: root

  moduleName: "omaimediateka"
  ipcTarget: "omaimediateka"

  // ---------- пути ----------
  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string pluginDir: decodeURIComponent(
    String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")).replace(/\/$/, "")
  readonly property string helper: pluginDir + "/bin/oma-mediateka"
  readonly property string cacheDir: (Quickshell.env("XDG_CACHE_HOME") || (home + "/.cache")) + "/omaimediateka"

  // ---------- палитра ----------
  readonly property color fg: root.barForeground
  readonly property color dim: Qt.darker(fg, 1.55)
  readonly property color accentC: Color.accent
  readonly property color urgentC: Color.urgent
  readonly property string fam: Style.font.family

  // ---------- состояние устройства ----------
  property var device: ({ state: "unknown", backend: "none", udid: "", mount: "", dcim: "", message: "" })
  property string deviceState: "unknown"
  property string deviceMessage: ""
  property string currentDcim: ""
  property bool scanning: false

  // ---------- данные ----------
  property var items: []
  property var shown: []
  property string query: ""
  property string sortOrder: "new"
  property int cursor: 0
  property int viewerIndex: -1
  property bool videoActive: false

  // ---------- поиск по содержимому ----------
  property var searchRows: []
  property string rowsQuery: ""
  property bool indexReady: false
  property bool indexRunning: false
  property string indexStatus: ""
  property string indexError: ""
  property bool searchRunning: false
  property string runningQuery: ""
  property bool searchQueued: false

  // ---------- превью ----------
  property var thumbState: ({})
  property var thumbRequested: ({})
  property var thumbQueue: []
  property string currentThumbKey: ""
  property var previewState: ({})
  property string previewKey: ""
  property string previewWantedKey: ""
  property bool previewReady: false
  property bool previewFailed: false
  property var videoMeta: null

  // ---------- экспорт ----------
  property string notice: ""
  property bool busy: false
  property var helperQueue: []
  property var currentJob: null
  property string helperOut: ""

  // ---------- выбор для пакетного экспорта ----------
  property var selection: ({})
  property int batchTotal: 0
  property int batchDone: 0
  property int batchErrors: 0
  readonly property int selectionCount: {
    var n = 0
    for (var k in root.selection) if (root.selection[k] === true) n++
    return n
  }

  readonly property real cellSize: Style.space(150)

  function togglePanel() { root.toggle() }

  function fileUrl(path) {
    var p = String(path || "")
    if (p === "") return ""
    return "file://" + p.replace(/%/g, "%25").replace(/ /g, "%20")
      .replace(/#/g, "%23").replace(/\?/g, "%3F")
  }

  // ---------- статус устройства ----------
  function refreshStatus() {
    if (statusProc.running) return
    statusProc.command = [root.helper, "status"]
    statusProc.running = true
  }

  function applyStatus(raw) {
    var d = null
    try { d = JSON.parse(String(raw || "")) } catch (e) { d = null }
    if (!d || typeof d.state !== "string") return
    var prev = root.deviceState
    root.device = d
    root.deviceState = d.state
    root.deviceMessage = String(d.message || "")
    if (d.state === "ready") {
      if (d.dcim !== root.currentDcim || prev !== "ready") {
        root.currentDcim = String(d.dcim || "")
        root.loadList()
      }
      if (prev !== "ready") indexKick.restart()
    } else {
      if (prev === "ready") root.resetMedia()
      root.currentDcim = ""
    }
  }

  function resetMedia() {
    root.items = []
    root.shown = []
    root.selection = ({})
    root.thumbState = ({})
    root.thumbRequested = ({})
    root.thumbQueue = []
    root.previewState = ({})
    root.previewReady = false
    root.viewerIndex = -1
    root.videoActive = false
    root.searchRows = []
    root.rowsQuery = ""
    root.searchQueued = false
  }

  Process {
    id: statusProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyStatus(String(text || ""))
    }
    stderr: StdioCollector { waitForEnd: true }
  }

  // ---------- листинг DCIM ----------
  function loadList() {
    if (listProc.running || root.currentDcim === "") return
    root.scanning = true
    listProc.command = [root.helper, "list", root.currentDcim]
    listProc.running = true
  }

  function applyList(raw) {
    root.items = Model.sortItems(Model.parseListing(String(raw || "")), root.sortOrder)
    root.thumbState = ({})
    root.thumbRequested = ({})
    root.thumbQueue = []
    root.selection = ({})
    root.recompute()
  }

  Process {
    id: listProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyList(String(text || ""))
    }
    stderr: StdioCollector { waitForEnd: true }
    onExited: root.scanning = false
  }

  function recompute() {
    var useIndex = root.indexReady && root.rowsQuery === root.query && Model.parseQuery(root.query).valid
    root.shown = useIndex
      ? Model.mergeSearch(root.items, root.searchRows, root.query)
      : Model.filterItems(root.items, root.query)
    if (root.cursor >= root.shown.length) root.cursor = Math.max(0, root.shown.length - 1)
    if (root.viewerIndex >= root.shown.length) root.viewerIndex = root.shown.length - 1
  }

  onQueryChanged: {
    root.recompute()
    searchDebounce.restart()
  }

  // ---------- поиск по содержимому: индекс Apple ----------
  // Индекс лежит в ~/.cache/omaimediateka/search.sqlite и пересобирается при
  // подключении телефона (инкрементально) или по кнопке. Пока он не готов,
  // фильтр ищет только по имени и папке и не ждёт индексацию.
  function runSearch() {
    if (root.deviceState !== "ready") return
    var q = Model.parseQuery(root.query)
    if (!root.indexReady || !q.valid) {
      root.searchRows = []
      root.rowsQuery = ""
      root.recompute()
      return
    }
    if (searchProc.running) { root.searchQueued = true; return }
    root.runningQuery = root.query
    root.searchRunning = true
    searchProc.command = [root.helper, "search", root.query]
    searchProc.running = true
  }

  function applySearch(raw) {
    if (root.runningQuery === root.query) {
      root.searchRows = Model.parseSearchRows(String(raw || ""))
      root.rowsQuery = root.runningQuery
    }
    root.recompute()
  }

  function startIndex() {
    if (root.indexRunning || root.deviceState !== "ready") return
    root.indexRunning = true
    root.indexStatus = "Проверяю индекс…"
    root.indexError = ""
    indexProc.command = [root.helper, "index"]
    indexProc.running = true
  }

  Timer {
    id: searchDebounce
    interval: 250
    onTriggered: root.runSearch()
  }

  // Даём интерфейсу сначала показать сетку и превью, и только потом качаем базы.
  Timer {
    id: indexKick
    interval: 1500
    onTriggered: root.startIndex()
  }

  Process {
    id: searchProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applySearch(String(text || ""))
    }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(code) {
      root.searchRunning = false
      if (root.searchQueued) { root.searchQueued = false; root.runSearch() }
    }
  }

  Process {
    id: indexProc
    stdout: SplitParser {
      onRead: function(line) {
        var t = String(line || "").trim()
        if (t !== "") root.indexStatus = t
      }
    }
    stderr: SplitParser {
      onRead: function(line) {
        var t = String(line || "").trim()
        if (t !== "") root.indexError = t
      }
    }
    onExited: function(code) {
      root.indexRunning = false
      if (code === 0) {
        var wasReady = root.indexReady
        root.indexReady = true
        root.indexError = ""
        root.indexStatus = ""
        if (!wasReady) root.showNotice("Поиск по содержимому готов")
        root.runSearch()
      } else {
        root.indexReady = false
        if (root.indexError === "")
          root.indexError = code === 4 ? "Нужен python3" : "Индекс Apple недоступен"
      }
    }
  }

  function setSort(order) {
    root.sortOrder = order
    root.items = Model.sortItems(root.items, order)
    root.recompute()
  }
  function toggleSort() { root.setSort(root.sortOrder === "new" ? "old" : "new") }

  // ---------- превью сетки (ленивая генерация, один процесс) ----------
  function thumbSource(item) {
    if (!item || root.thumbState[item.key] !== true) return ""
    return root.fileUrl(Model.thumbPath(root.cacheDir, item, "thumb"))
  }

  function requestThumb(item) {
    if (!item || !item.key) return
    if (root.thumbState[item.key] === true) return
    if (root.thumbRequested[item.key] === true) return
    var req = {}
    for (var k in root.thumbRequested) req[k] = root.thumbRequested[k]
    req[item.key] = true
    root.thumbRequested = req
    var q = root.thumbQueue.slice()
    q.push({
      key: item.key,
      path: item.path,
      dst: Model.thumbPath(root.cacheDir, item, "thumb"),
      kind: item.kind
    })
    root.thumbQueue = q
    root.pumpThumb()
  }

  function pumpThumb() {
    if (thumbProc.running) return
    var q = root.thumbQueue.slice()
    while (q.length > 0) {
      var job = q.shift()
      if (root.thumbState[job.key] === true) continue
      root.thumbQueue = q
      root.currentThumbKey = job.key
      thumbProc.command = [root.helper, "thumb", job.path, job.dst, job.kind, "thumb"]
      thumbProc.running = true
      return
    }
    root.thumbQueue = q
  }

  function markThumb(key) {
    if (key === "") return
    var st = {}
    for (var k in root.thumbState) st[k] = root.thumbState[k]
    st[key] = true
    root.thumbState = st
  }

  Process {
    id: thumbProc
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) {
      var key = root.currentThumbKey
      root.currentThumbKey = ""
      if (exitCode === 0) root.markThumb(key)
      root.pumpThumb()
    }
  }

  // ---------- просмотр ----------
  function currentItem() {
    if (root.viewerIndex < 0 || root.viewerIndex >= root.shown.length) return null
    return root.shown[root.viewerIndex]
  }
  function previewPathOf(item) {
    if (!item) return ""
    return Model.thumbPath(root.cacheDir, item, "preview")
  }

  function openViewer(index) {
    if (index < 0 || index >= root.shown.length) return
    root.viewerIndex = index
    root.cursor = index
    root.videoActive = false
    // Уводим фокус из строки фильтра: иначе открытие кадра по Enter оставляет
    // фокус в поле, и горячие клавиши экспорта в просмотрщике отключены.
    keyCatcher.forceActiveFocus()
    root.updatePreview()
  }
  function closeViewer() {
    root.viewerIndex = -1
    root.videoActive = false
    root.previewReady = false
    root.videoMeta = null
  }
  function viewerStep(delta) {
    if (root.viewerIndex < 0) return
    var n = root.shown.length
    if (n === 0) return
    var next = Math.max(0, Math.min(n - 1, root.viewerIndex + delta))
    if (next === root.viewerIndex) return
    root.viewerIndex = next
    root.cursor = next
    root.videoActive = false
    root.updatePreview()
  }

  function updatePreview() {
    var it = root.currentItem()
    root.previewReady = false
    root.previewFailed = false
    root.videoMeta = null
    if (!it) { root.previewWantedKey = ""; return }
    root.previewWantedKey = it.key
    if (root.previewState[it.key] === true) {
      root.previewReady = true
      if (it.kind === "video") root.loadMeta(it)
      return
    }
    if (!previewProc.running) root.startPreview(it)
  }

  function startPreview(it) {
    if (!it) return
    root.previewKey = it.key
    previewProc.command = [root.helper, "thumb", it.path, root.previewPathOf(it), it.kind, "preview"]
    previewProc.running = true
  }

  function markPreview(key) {
    if (key === "") return
    var st = {}
    for (var k in root.previewState) st[k] = root.previewState[k]
    st[key] = true
    root.previewState = st
  }

  Process {
    id: previewProc
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) {
      var key = root.previewKey
      var wanted = root.previewWantedKey
      var cur = root.currentItem()
      if (exitCode === 0) root.markPreview(key)
      if (wanted !== "" && cur && cur.key === wanted && wanted !== key) {
        root.startPreview(cur)
        return
      }
      if (wanted === key && root.viewerIndex >= 0 && cur && cur.key === key) {
        if (exitCode === 0) {
          root.previewReady = true
          if (cur.kind === "video") root.loadMeta(cur)
        } else {
          root.previewFailed = true
        }
      }
    }
  }

  // ---------- метаданные видео ----------
  function loadMeta(item) {
    if (!item || item.kind !== "video") return
    if (metaProc.running) { root.metaPending = true; return }
    root.metaKey = item.key
    metaProc.command = [root.helper, "meta", item.path]
    metaProc.running = true
  }
  property bool metaPending: false
  property string metaKey: ""

  function applyMeta(raw) {
    var parts = String(raw || "").trim().split("\t")
    var dur = parseFloat(parts[0])
    root.videoMeta = {
      duration: isFinite(dur) ? dur : 0,
      width: parseInt(parts[1], 10) || 0,
      height: parseInt(parts[2], 10) || 0
    }
  }

  Process {
    id: metaProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyMeta(String(text || ""))
    }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) {
      if (root.metaPending) { root.metaPending = false; root.loadMeta(root.currentItem()) }
    }
  }

  function toggleVideo() {
    var it = root.currentItem()
    if (!it || it.kind !== "video") return
    root.videoActive = !root.videoActive
  }

  // ---------- экспорт: сохранить и в буфер ----------
  function showNotice(text) {
    root.notice = String(text || "")
    noticeTimer.restart()
  }

  // Элемент, к которому применяются действия: открытый кадр или курсор сетки.
  function actionItem() {
    if (root.viewerIndex >= 0) return root.currentItem()
    if (root.shown.length === 0) return null
    var i = Math.max(0, Math.min(root.shown.length - 1, root.cursor))
    return root.shown[i]
  }

  function enqueueJob(op, args, extra) {
    var q = root.helperQueue.slice()
    q.push({ op: op, args: args, extra: extra || {} })
    root.helperQueue = q
    root.pumpHelper()
  }

  function pumpHelper() {
    if (helperProc.running) return
    var q = root.helperQueue.slice()
    if (q.length === 0) { root.busy = false; return }
    var job = q.shift()
    root.helperQueue = q
    root.currentJob = job
    root.busy = true
    helperProc.command = [root.helper, job.op].concat(job.args)
    helperProc.running = true
  }

  function handleHelperResult(job, code, out) {
    if (!job) return
    if (job.op === "names") {
      if (code !== 0) { root.showNotice("Не удалось прочитать папку"); return }
      var taken = {}
      var lines = String(out || "").split("\n")
      for (var i = 0; i < lines.length; i++) if (lines[i] !== "") taken[lines[i]] = true
      var items = job.extra.items || (job.extra.item ? [job.extra.item] : [])
      if (items.length === 0) return
      var dir = job.extra.dir
      var rawNames = []
      for (var j = 0; j < items.length; j++) rawNames.push(items[j].name)
      var names = Model.allocateNames(rawNames, taken)
      root.batchTotal = items.length
      root.batchDone = 0
      root.batchErrors = 0
      for (var c = 0; c < items.length; c++)
        root.enqueueJob("copy", [items[c].path, Model.destPath(dir, names[c])], { item: items[c], name: names[c] })
    } else if (job.op === "copy") {
      root.batchDone++
      if (code !== 0) root.batchErrors++
      if (root.batchDone >= root.batchTotal) {
        if (root.batchErrors === 0 && root.batchTotal === 1) root.showNotice("Сохранено: " + job.extra.name)
        else if (root.batchErrors === 0) root.showNotice("Сохранено: " + root.batchTotal + " " + Model.fileWord(root.batchTotal))
        else root.showNotice("Сохранено: " + (root.batchTotal - root.batchErrors) + " из " + root.batchTotal)
        root.batchTotal = 0
        root.batchDone = 0
        root.batchErrors = 0
      }
    } else if (job.op === "clip-image") {
      if (code === 0) root.showNotice("Изображение в буфере")
      else if (code === 4) root.showNotice("wl-copy не найден")
      else root.showNotice("Не удалось скопировать изображение")
    } else if (job.op === "clip-text") {
      if (code === 0) root.showNotice(root.selectionCount > 1 ? "Пути в буфере" : "Путь к файлу в буфере")
      else if (code === 4) root.showNotice("wl-copy не найден")
      else root.showNotice("Не удалось скопировать")
    } else if (job.op === "pick-dir") {
      var chosen = String(out || "").trim()
      if (code === 0 && chosen !== "") root.enqueueJob("names", [chosen], { items: job.extra.items, dir: chosen })
      else root.showNotice("Сохранение отменено")
    }
  }

  // ---------- выбор ----------
  function toggleSelect(item) {
    if (!item || !item.key) return
    var s = {}
    for (var k in root.selection) s[k] = root.selection[k]
    if (s[item.key] === true) delete s[item.key]
    else s[item.key] = true
    root.selection = s
  }
  function clearSelection() { root.selection = ({}) }
  function selectAll() {
    var s = {}
    for (var i = 0; i < root.shown.length; i++) s[root.shown[i].key] = true
    root.selection = s
  }
  function selectedItems() {
    var out = []
    for (var i = 0; i < root.shown.length; i++)
      if (root.selection[root.shown[i].key] === true) out.push(root.shown[i])
    return out
  }
  // Элементы для действий: сначала выделение, иначе курсор/открытый кадр.
  function actionItems() {
    var s = root.selectedItems()
    if (s.length > 0) return s
    var one = root.actionItem()
    return one ? [one] : []
  }

  // Сохранить оригиналы без перекодирования. askDir=true — диалог выбора папки.
  function saveItems(items, askDir) {
    if (!items || items.length === 0) { root.showNotice("Нечего сохранять"); return }
    if (askDir) {
      root.enqueueJob("pick-dir", [root.home + "/Pictures"], { items: items })
    } else {
      var dir = Model.defaultExportDir(root.home)
      root.enqueueJob("names", [dir], { items: items, dir: dir })
    }
  }

  // Один элемент: фото — картинкой, прочее — путём. Пачка — список путей текстом.
  function copyItems(items) {
    if (!items || items.length === 0) { root.showNotice("Нечего копировать"); return }
    if (items.length === 1) {
      if (items[0].kind === "photo") root.enqueueJob("clip-image", [items[0].path], { item: items[0] })
      else root.enqueueJob("clip-text", [items[0].path], { item: items[0] })
      return
    }
    var paths = []
    for (var i = 0; i < items.length; i++) paths.push(items[i].path)
    root.enqueueJob("clip-text", [paths.join("\n")], { items: items })
  }

  Process {
    id: helperProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.helperOut = String(text || "")
    }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(code) {
      var job = root.currentJob
      root.currentJob = null
      root.handleHelperResult(job, code, root.helperOut)
      root.helperOut = ""
      root.pumpHelper()
    }
  }

  Timer {
    id: noticeTimer
    interval: 2600
    onTriggered: root.notice = ""
  }

  // ---------- подписи состояний ----------
  function stateTitle(s) {
    if (s === "no-tools") return "Нет libimobiledevice"
    if (s === "no-device") return "iPhone не подключён"
    if (s === "unpaired") return "Нужно доверие"
    if (s === "no-backend") return "Нужен доступ к файлам"
    if (s === "mounting") return "Подключение…"
    if (s === "no-dcim") return "Нет доступа к DCIM"
    if (s === "error") return "Ошибка"
    if (s === "ready") return "Готово"
    return "iPhone"
  }

  function stateHint(s) {
    if (s === "no-tools") return "sudo pacman -S libimobiledevice usbmuxd"
    if (s === "no-backend") return "sudo pacman -S gvfs-afc\nзатем переподключите кабель"
    if (s === "unpaired") return "Если окно доверия не появилось: idevicepair -u <UDID> pair"
    return ""
  }

  // ---------- клавиатура ----------
  function moveCursor(dx, dy) {
    var n = root.shown.length
    if (n === 0) { root.cursor = 0; return }
    var cols = Math.max(1, root.columns)
    var c = root.cursor
    if (dy !== 0) c += dy * cols
    if (dx !== 0) c += dx
    root.cursor = Math.max(0, Math.min(n - 1, c))
    grid.positionViewAtIndex(root.cursor, GridView.Contain)
  }

  function activateCursor() {
    if (root.viewerIndex >= 0) { root.toggleVideo(); return }
    root.openViewer(root.cursor)
  }

  function handleTextKey(t) {
    if (root.viewerIndex >= 0) {
      if (t === "p" || t === "P" || t === " ") root.toggleVideo()
      return
    }
    if (t.length !== 1) return
    filterField.text = filterField.text + t
    root.query = filterField.text
    filterField.forceActiveFocus()
    filterField.cursorPosition = filterField.text.length
  }

  // ---------- таймер опроса ----------
  Timer {
    id: pollTimer
    interval: root.opened ? 2500 : 6000
    repeat: true
    running: true
    onTriggered: root.refreshStatus()
  }

  onOpenedChanged: {
    if (root.opened) {
      root.refreshStatus()
      if (root.deviceState === "ready" && root.items.length === 0) root.loadList()
    }
  }

  Component.onCompleted: root.refreshStatus()

  // ---------- кнопка в баре ----------
  // Активный бар (salehardec.bar) кладёт `chipSpacing` между виджетами одной
  // секции, но не оставляет его на границе с центральным «якорем». Из-за этого
  // пилюля, прижатая справа к якорю, получает слева зазор на весь chipSpacing
  // больше, чем справа. Сдвигаем пилюлю на половину этого зазора влево и на
  // столько же сужаем слот — тогда видимые зазоры с обеих сторон совпадают с
  // соседними виджетами. На баре без chipSpacing (штатный omarchy.bar)
  // компенсация выключена.
  readonly property real barChipSpacing: (root.bar && typeof root.bar.chipSpacing === "number")
    ? root.bar.chipSpacing : 0
  readonly property real pillShift: barChipSpacing / 2
  readonly property real pillSlotTrim: barChipSpacing > 0 ? 4 : 0

  implicitWidth: pill.implicitWidth - pillSlotTrim
  implicitHeight: root.barSize || Style.space(26)

  Item {
    id: pill
    implicitWidth: pillContent.implicitWidth + Style.space(18)
    implicitHeight: Math.max(Style.space(20), (root.barSize || Style.space(26)) - Style.space(6))
    anchors.centerIn: parent
    anchors.horizontalCenterOffset: -root.pillShift

    Rectangle {
      anchors.fill: parent
      radius: Style.space(8)
      color: root.accentC
      opacity: root.opened ? 1 : (root.deviceState === "ready" ? 0.92 : 0.55)
      Behavior on opacity { NumberAnimation { duration: 120 } }
    }
    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.togglePanel()
    }
    RowLayout {
      id: pillContent
      anchors.centerIn: parent
      spacing: Style.space(7)

      Text {
        textFormat: Text.PlainText
        text: "\uf03d"
        color: Color.background
        font.family: root.fam
        font.pixelSize: Style.font.bodySmall
        Layout.alignment: Qt.AlignVCenter
      }
      Text {
        textFormat: Text.PlainText
        text: "iPhone"
        color: Color.background
        font.family: root.fam
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        Layout.alignment: Qt.AlignVCenter
      }
      Rectangle {
        Layout.minimumWidth: Math.max(Style.space(19), chipText.implicitWidth + Style.space(8))
        Layout.preferredHeight: Style.space(15)
        Layout.alignment: Qt.AlignVCenter
        radius: Style.space(8)
        color: Color.background
        Text {
          id: chipText
          anchors.centerIn: parent
          textFormat: Text.PlainText
          text: root.deviceState === "ready" ? String(root.items.length) : "—"
          color: root.accentC
          font.family: root.fam
          font.pixelSize: Style.font.caption
          font.bold: true
        }
      }
    }
  }

  // ---------- попап ----------
  KeyboardPanel {
    id: panel
    anchorItem: pill
    owner: root
    bar: root.bar
    open: root.opened
    centerOnBar: true
    focusTarget: keyCatcher
    contentWidth: root.viewerIndex >= 0
      ? panel.availableCardWidth
      : panel.fittedContentWidth(Style.space(780))
    contentHeight: root.viewerIndex >= 0
      ? panel.availableCardHeight
      : panel.fittedContentHeight(Style.space(560), Style.space(720))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: filterField.activeFocus

      onCloseRequested: {
        if (root.viewerIndex >= 0) root.closeViewer()
        else if (root.selectionCount > 0) root.clearSelection()
        else root.close()
      }
      onActivateRequested: root.activateCursor()
      onMoveRequested: function(dx, dy) {
        if (root.viewerIndex >= 0) {
          if (dx !== 0) root.viewerStep(dx)
          return
        }
        root.moveCursor(dx, dy)
      }
      onTextKey: function(text) { root.handleTextKey(text) }

      // Горячие клавиши экспорта (в поле фильтра не перехватываем — там свой Ctrl+C).
      Shortcut {
        sequence: "Ctrl+S"
        enabled: root.opened && !filterField.activeFocus
        onActivated: root.saveItems(root.actionItems(), false)
      }
      Shortcut {
        sequence: "Ctrl+Shift+S"
        enabled: root.opened && !filterField.activeFocus
        onActivated: root.saveItems(root.actionItems(), true)
      }
      Shortcut {
        sequence: "Ctrl+C"
        enabled: root.opened && !filterField.activeFocus
        onActivated: root.copyItems(root.actionItems())
      }
      Shortcut {
        sequence: "Space"
        enabled: root.opened && root.viewerIndex < 0 && !filterField.activeFocus
        onActivated: root.toggleSelect(root.actionItem())
      }
      Shortcut {
        sequence: "Ctrl+A"
        enabled: root.opened && root.viewerIndex < 0 && !filterField.activeFocus
        onActivated: root.selectAll()
      }

      // ================= сетка =================
      Item {
        id: gridPage
        anchors.fill: parent
        visible: root.viewerIndex < 0

        ColumnLayout {
          anchors.fill: parent
          spacing: Style.space(6)

          PanelHero {
            Layout.fillWidth: true
            title: "Медиатека iPhone"
            meta: {
              if (root.deviceState !== "ready") return "USB"
              var p = Model.countKind(root.items, "photo")
              var v = Model.countKind(root.items, "video")
              var base = p + " фото · " + v + " видео"
              if (root.scanning) base += " · обновление…"
              return base
            }
            foreground: root.fg
            fontFamily: root.fam
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: "\uf03d"
                color: root.fg
                font.family: root.fam
                font.pixelSize: Style.font.display
              }
            }
          }

          // --- состояние «не готово» ---
          ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.deviceState !== "ready"
            spacing: Style.space(10)

            Item { Layout.fillHeight: true }

            Text {
              textFormat: Text.PlainText
              Layout.alignment: Qt.AlignHCenter
              text: root.deviceState === "mounting" ? "\uf110" : "\uf3e7"
              color: root.dim
              font.family: root.fam
              font.pixelSize: Style.font.displayLarge
            }
            Text {
              textFormat: Text.PlainText
              Layout.fillWidth: true
              horizontalAlignment: Text.AlignHCenter
              text: root.stateTitle(root.deviceState)
              color: root.fg
              font.family: root.fam
              font.pixelSize: Style.font.title
              font.bold: true
            }
            Text {
              textFormat: Text.PlainText
              Layout.fillWidth: true
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.WordWrap
              visible: root.deviceMessage !== ""
              text: root.deviceMessage
              color: root.dim
              font.family: root.fam
              font.pixelSize: Style.font.bodySmall
            }
            Text {
              textFormat: Text.PlainText
              Layout.fillWidth: true
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.WordWrap
              visible: root.stateHint(root.deviceState) !== ""
              text: root.stateHint(root.deviceState)
              color: root.accentC
              font.family: root.fam
              font.pixelSize: Style.font.caption
            }
            Button {
              Layout.alignment: Qt.AlignHCenter
              text: "Проверить снова"
              foreground: root.fg
              fontFamily: root.fam
              fontSize: Style.font.bodySmall
              horizontalPadding: Style.space(12)
              verticalPadding: Style.space(6)
              onClicked: root.refreshStatus()
            }
            Item { Layout.fillHeight: true }
          }

          // --- фильтр и сортировка ---
          RowLayout {
            Layout.fillWidth: true
            visible: root.deviceState === "ready"
            spacing: Style.space(6)

            TextField {
              id: filterField
              Layout.fillWidth: true
              placeholderText: root.indexReady
                ? "имя файла, папка, содержимое…"
                : "имя файла, папка…"
              foreground: root.fg
              font.family: root.fam
              horizontalPadding: Style.space(10)
              verticalPadding: Style.space(7)
              onTextEdited: root.query = text
              onAccepted: if (root.shown.length > 0) root.openViewer(root.cursor)
              Keys.onEscapePressed: {
                text = ""
                root.query = ""
                keyCatcher.forceActiveFocus()
              }
              Keys.onDownPressed: function(event) {
                event.accepted = true
                keyCatcher.forceActiveFocus()
              }
            }
            Button {
              text: root.sortOrder === "new" ? "Сначала новые" : "Сначала старые"
              foreground: root.fg
              fontFamily: root.fam
              fontSize: Style.font.caption
              horizontalPadding: Style.space(8)
              verticalPadding: Style.space(6)
              focusable: false
              onClicked: root.toggleSort()
            }
            Button {
              text: root.indexRunning ? "Индексация…" : "Обновить поиск"
              enabled: !root.indexRunning
              foreground: root.fg
              fontFamily: root.fam
              fontSize: Style.font.caption
              horizontalPadding: Style.space(8)
              verticalPadding: Style.space(6)
              focusable: false
              onClicked: root.startIndex()
            }
          }

          // --- состояние индекса поиска по содержимому ---
          Text {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            visible: root.indexRunning || root.indexError !== ""
            elide: Text.ElideRight
            text: root.indexRunning
              ? (root.indexStatus !== "" ? root.indexStatus : "Индексирую…")
              : root.indexError
            color: root.indexError !== "" ? root.urgentC : root.dim
            font.family: root.fam
            font.pixelSize: Style.font.caption
          }

          // --- сетка ---
          GridView {
            id: grid
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.deviceState === "ready"
            clip: true
            cellWidth: root.cellSize
            cellHeight: root.cellSize
            cacheBuffer: root.cellSize * 2
            boundsBehavior: Flickable.StopAtBounds
            model: root.shown
            onWidthChanged: root.columns = Math.max(1, Math.floor(width / cellWidth))
            delegate: GridCell {}

            ScrollBar.vertical: ScrollBar {
              policy: grid.contentHeight > grid.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            }
          }

          // --- пустой результат ---
          Text {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            visible: root.deviceState === "ready" && root.shown.length === 0
            text: root.query !== "" ? "Ничего не найдено"
              : (root.scanning ? "Читаю DCIM…" : "В DCIM нет фото и видео")
            color: root.dim
            font.family: root.fam
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
          }

          // --- футер ---
          ColumnLayout {
            Layout.fillWidth: true
            spacing: Style.space(2)

            RowLayout {
              Layout.fillWidth: true
              spacing: Style.space(10)
              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                elide: Text.ElideRight
                text: root.notice !== ""
                  ? root.notice
                  : (root.deviceState === "ready"
                    ? (root.cursor >= 0 && root.cursor < root.shown.length && root.shown[root.cursor]
                      ? root.shown[root.cursor].name
                      : "iPhone")
                    : root.device.udid !== "" ? root.device.udid : "USB")
                color: root.notice !== "" ? root.accentC : root.dim
                font.family: root.fam
                font.pixelSize: Style.font.caption
              }
              Text {
                textFormat: Text.PlainText
                text: root.busy ? "Занято…" : "Enter открыть · ← → ↑ ↓ · Esc закрыть"
                color: root.busy ? root.accentC : root.dim
                font.family: root.fam
                font.pixelSize: Style.font.caption
              }
            }

            RowLayout {
              Layout.fillWidth: true
              visible: root.deviceState === "ready" && root.shown.length > 0
              spacing: Style.space(8)

              ExportActions {
                items: root.actionItems()
                enabled: !root.busy
              }
              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                elide: Text.ElideRight
                horizontalAlignment: Text.AlignRight
                text: root.selectionCount > 0
                  ? ("Выбрано: " + root.selectionCount + " · Space снять · Ctrl+A всё")
                  : "Space выбрать · Ctrl+A всё · Ctrl+S сохранить · Ctrl+C буфер"
                color: root.selectionCount > 0 ? root.fg : root.dim
                font.family: root.fam
                font.pixelSize: Style.font.caption
              }
            }
          }
        }
      }

      // ================= просмотр =================
      Item {
        id: viewerPage
        anchors.fill: parent
        visible: root.viewerIndex >= 0

        Rectangle {
          anchors.fill: parent
          color: Color.background
          opacity: 0.55
          radius: Style.cornerRadius
        }

        Image {
          id: previewImg
          anchors.fill: parent
          anchors.margins: Style.space(10)
          asynchronous: true
          cache: false
          fillMode: Image.PreserveAspectFit
          source: root.previewReady ? root.fileUrl(root.previewPathOf(root.currentItem())) : ""
          visible: !root.videoActive
        }

        Loader {
          id: videoLoader
          anchors.fill: parent
          anchors.margins: Style.space(10)
          active: root.videoActive
          source: root.pluginDir + "/VideoView.qml"
          onLoaded: {
            if (!item) return
            var cur = root.currentItem()
            if (cur) {
              item.source = root.fileUrl(cur.path)
              item.autoPlay = true
              item.play()
            }
          }
        }

        // индикатор загрузки превью
        Text {
          anchors.centerIn: parent
          textFormat: Text.PlainText
          visible: !root.previewReady && !root.videoActive && !root.previewFailed
          text: "Загрузка…"
          color: root.dim
          font.family: root.fam
          font.pixelSize: Style.font.bodySmall
        }

        Text {
          anchors.centerIn: parent
          textFormat: Text.PlainText
          visible: root.previewFailed
          text: "Не удалось открыть"
          color: root.urgentC
          font.family: root.fam
          font.pixelSize: Style.font.bodySmall
        }

        // ошибка видео
        Text {
          anchors.centerIn: parent
          anchors.verticalCenterOffset: Style.space(40)
          textFormat: Text.PlainText
          visible: videoLoader.status === Loader.Error
          text: "Воспроизведение недоступно"
          color: root.urgentC
          font.family: root.fam
          font.pixelSize: Style.font.caption
        }

        // верхняя панель: подложка-скрим, чтобы текст и кнопки читались на любом кадре
        Rectangle {
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.margins: Style.space(2)
          height: viewerHeaderRow.implicitHeight + Style.space(8)
          radius: Style.cornerRadius
          color: Util.alpha(Color.background, 0.88)
          border.width: Style.space(1)
          border.color: Util.alpha(root.fg, 0.15)
        }
        RowLayout {
          id: viewerHeaderRow
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.margins: Style.space(6)
          spacing: Style.space(8)

          Text {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            elide: Text.ElideRight
            text: {
              var it = root.currentItem()
              if (!it) return ""
              var parts = [it.name]
              if (it.mtime > 0) parts.push(Model.formatDate(it.mtime) + " " + Model.formatTime(it.mtime))
              parts.push(Model.formatSize(it.size))
              if (it.kind === "video" && root.videoMeta && root.videoMeta.duration > 0)
                parts.push(Model.formatDuration(root.videoMeta.duration))
              if (Model.badgeLabel(it) !== "") parts.push(Model.badgeLabel(it))
              return parts.join("  ·  ")
            }
            color: root.fg
            font.family: root.fam
            font.pixelSize: Style.font.bodySmall
            font.bold: true
          }
          Text {
            textFormat: Text.PlainText
            text: String(root.viewerIndex + 1) + " / " + String(root.shown.length)
            color: root.fg
            font.family: root.fam
            font.pixelSize: Style.font.caption
          }
          Button {
            visible: {
              var it = root.currentItem()
              return !!it && it.kind === "video"
            }
            text: root.videoActive ? "Пауза" : "Играть"
            foreground: root.fg
            fontFamily: root.fam
            fontSize: Style.font.caption
            horizontalPadding: Style.space(8)
            verticalPadding: Style.space(5)
            focusable: false
            onClicked: root.toggleVideo()
          }
          ExportActions {
            items: root.currentItem() ? [root.currentItem()] : []
            enabled: !root.busy
          }
          Button {
            text: "Закрыть"
            foreground: root.fg
            fontFamily: root.fam
            fontSize: Style.font.caption
            horizontalPadding: Style.space(8)
            verticalPadding: Style.space(5)
            focusable: false
            onClicked: root.closeViewer()
          }
        }

        // индикатор занятости / уведомление экспорта (на подложке)
        Rectangle {
          anchors.horizontalCenter: parent.horizontalCenter
          anchors.bottom: parent.bottom
          anchors.bottomMargin: Style.space(6)
          width: viewerNotice.implicitWidth + Style.space(16)
          height: viewerNotice.implicitHeight + Style.space(8)
          radius: Style.cornerRadius
          color: Util.alpha(Color.background, 0.88)
          border.width: Style.space(1)
          border.color: Util.alpha(root.fg, 0.15)
          visible: root.busy || root.notice !== ""

          Text {
            id: viewerNotice
            anchors.centerIn: parent
            textFormat: Text.PlainText
            text: root.busy ? "Занято…" : root.notice
            color: root.fg
            font.family: root.fam
            font.pixelSize: Style.font.bodySmall
            font.bold: true
          }
        }

        // навигация по краям
        Button {
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          anchors.leftMargin: Style.space(6)
          visible: root.viewerIndex > 0
          text: "‹"
          background: Util.alpha(Color.background, 0.88)
          foreground: root.fg
          fontFamily: root.fam
          fontSize: Style.font.title
          horizontalPadding: Style.space(10)
          verticalPadding: Style.space(10)
          focusable: false
          onClicked: root.viewerStep(-1)
        }
        Button {
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.rightMargin: Style.space(6)
          visible: root.viewerIndex >= 0 && root.viewerIndex < root.shown.length - 1
          text: "›"
          background: Util.alpha(Color.background, 0.88)
          foreground: root.fg
          fontFamily: root.fam
          fontSize: Style.font.title
          horizontalPadding: Style.space(10)
          verticalPadding: Style.space(10)
          focusable: false
          onClicked: root.viewerStep(1)
        }
      }
    }
  }

  // ---------- ячейка сетки ----------
  component GridCell: CursorSurface {
    id: cell

    required property var modelData
    required property int index

    readonly property bool isVideo: !!modelData && modelData.kind === "video"
    readonly property bool selected: !!modelData && root.selection[modelData.key] === true

    width: grid.cellWidth
    height: grid.cellHeight
    hasCursor: cellHover.hovered || root.cursor === index

    Rectangle {
      anchors.fill: parent
      anchors.margins: Style.space(3)
      radius: Style.cornerRadius
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.06)
      clip: true

      Image {
        id: thumbImg
        anchors.fill: parent
        asynchronous: true
        cache: false
        fillMode: Image.PreserveAspectCrop
        sourceSize.width: root.cellSize
        sourceSize.height: root.cellSize
        source: root.thumbSource(cell.modelData)
        visible: status === Image.Ready
      }

      Text {
        anchors.centerIn: parent
        textFormat: Text.PlainText
        visible: thumbImg.status !== Image.Ready
        text: cell.isVideo ? "\uf03d" : "\uf03e"
        color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.35)
        font.family: root.fam
        font.pixelSize: Style.font.display
      }

      // видеомаркер
      Rectangle {
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        anchors.margins: Style.space(4)
        visible: cell.isVideo
        width: playIcon.implicitWidth + Style.space(8)
        height: playIcon.implicitHeight + Style.space(3)
        radius: Style.space(4)
        color: Util.alpha(Color.background, 0.65)
        Text {
          id: playIcon
          anchors.centerIn: parent
          textFormat: Text.PlainText
          text: "\uf04b"
          color: Color.foreground
          font.family: root.fam
          font.pixelSize: Style.font.caption
        }
      }
    }

    // бейдж источника: почему элемент в выдаче поиска по содержимому
    Rectangle {
      anchors.top: parent.top
      anchors.right: parent.right
      anchors.margins: Style.space(4)
      visible: badgeText.text !== ""
      width: Math.min(cell.width - Style.space(12), badgeText.implicitWidth + Style.space(10))
      height: badgeText.implicitHeight + Style.space(5)
      radius: Style.space(4)
      color: Util.alpha(Color.background, 0.72)
      border.width: Style.space(1)
      border.color: Util.alpha(root.fg, 0.18)
      z: 5
      Text {
        id: badgeText
        anchors.centerIn: parent
        width: parent.width - Style.space(6)
        textFormat: Text.PlainText
        horizontalAlignment: Text.AlignHCenter
        elide: Text.ElideRight
        text: cell.modelData ? Model.badgeLabel(cell.modelData) : ""
        color: root.fg
        font.family: root.fam
        font.pixelSize: Style.font.caption
        font.bold: true
      }
    }

    // рамка выбранной ячейки
    Rectangle {
      anchors.fill: parent
      anchors.margins: Style.space(2)
      radius: Style.cornerRadius
      color: "transparent"
      border.width: Style.space(2)
      border.color: root.accentC
      visible: cell.selected
      z: 4
    }

    HoverHandler { id: cellHover }

    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.LeftButton
      cursorShape: Qt.PointingHandCursor
      onClicked: function(mouse) {
        if (mouse.modifiers & Qt.ControlModifier) root.toggleSelect(cell.modelData)
        else root.openViewer(cell.index)
      }
    }

    // галочка выбора (клик по ней не открывает кадр)
    Rectangle {
      id: selectBadge
      anchors.top: parent.top
      anchors.left: parent.left
      anchors.margins: Style.space(5)
      width: Style.space(18)
      height: Style.space(18)
      radius: Style.space(4)
      color: cell.selected ? root.accentC : Util.alpha(Color.background, 0.6)
      border.width: Style.space(1)
      border.color: Util.alpha(root.fg, 0.55)
      visible: cellHover.hovered || cell.selected
      z: 6
      Text {
        anchors.centerIn: parent
        textFormat: Text.PlainText
        text: cell.selected ? "\uf00c" : ""
        color: Color.background
        font.family: root.fam
        font.pixelSize: Style.font.caption
        font.bold: true
      }
      MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: function(mouse) {
          root.toggleSelect(cell.modelData)
          mouse.accepted = true
        }
      }
    }

    Component.onCompleted: root.requestThumb(cell.modelData)
    onModelDataChanged: if (cell.modelData) root.requestThumb(cell.modelData)
  }

  // ---------- кнопки экспорта ----------
  component ExportActions: RowLayout {
    id: exportRow
    property var items: []
    readonly property bool canAct: items.length > 0 && exportRow.enabled
    readonly property string saveLabel: root.selectionCount > 0
      ? ("Сохранить (" + root.selectionCount + ")") : "Сохранить"
    spacing: Style.space(6)

    Button {
      text: exportRow.saveLabel
      enabled: exportRow.canAct
      foreground: root.fg
      fontFamily: root.fam
      fontSize: Style.font.caption
      horizontalPadding: Style.space(8)
      verticalPadding: Style.space(4)
      focusable: false
      onClicked: root.saveItems(exportRow.items, false)
    }
    Button {
      text: "Сохранить как"
      enabled: exportRow.canAct
      foreground: root.fg
      fontFamily: root.fam
      fontSize: Style.font.caption
      horizontalPadding: Style.space(8)
      verticalPadding: Style.space(4)
      focusable: false
      onClicked: root.saveItems(exportRow.items, true)
    }
    Button {
      text: "В буфер"
      enabled: exportRow.canAct
      foreground: root.fg
      fontFamily: root.fam
      fontSize: Style.font.caption
      horizontalPadding: Style.space(8)
      verticalPadding: Style.space(4)
      focusable: false
      onClicked: root.copyItems(exportRow.items)
    }
    Button {
      text: root.selectionCount > 0 ? "Снять выбор" : "Выбрать всё"
      enabled: exportRow.enabled && (root.selectionCount > 0 || root.shown.length > 0)
      visible: root.viewerIndex < 0
      foreground: root.fg
      fontFamily: root.fam
      fontSize: Style.font.caption
      horizontalPadding: Style.space(8)
      verticalPadding: Style.space(4)
      focusable: false
      onClicked: root.selectionCount > 0 ? root.clearSelection() : root.selectAll()
    }
  }

  // количество колонок (нужно moveCursor)
  property int columns: 4
}
