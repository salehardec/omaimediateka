import QtQuick
import QtMultimedia

// Изолированный плеер видео для просмотрщика. Вынесен в отдельный файл,
// чтобы импорт QtMultimedia не влиял на загрузку основного Panel.qml.
Item {
  id: root

  property url source: ""
  property bool autoPlay: true
  property string errorText: ""

  function play() { video.play() }
  function pause() { video.pause() }
  function stop() { video.stop() }

  Video {
    id: video
    anchors.fill: parent
    source: root.source
    autoPlay: root.autoPlay
    fillMode: VideoOutput.PreserveAspectFit
    loops: 1
    onErrorOccurred: function(error, errorString) {
      root.errorText = String(errorString || "")
    }
  }
}
