pragma ComponentBehavior: Bound
import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui as Ui

// Bar indicator for local LLMs. Dim chip while nothing is loaded; accent chip
// plus resident GPU memory while a runtime holds a model; the chip pulses
// while the GPU is generating. Left click opens the detail panel.
Ui.BarWidget {
  id: root
  moduleName: "ubruckhaus.llm-usage"

  property var stats: ({})
  // Push every snapshot into the panel; never let it read from an older widget instance.
  onStatsChanged: if (panelLoader.item && "stats" in panelLoader.item) panelLoader.item.stats = root.stats
  readonly property bool active: stats.active === true
  readonly property bool busy: stats.busy === true
  readonly property string scriptDir: Qt.resolvedUrl("scripts").toString().replace(/^file:\/\//, "")

  function gib(bytes) { return (Number(bytes || 0) / 1073741824).toFixed(1) }

  readonly property string barText: {
    if (!active) return "󰘚"
    // "!" marks model memory that was evicted from VRAM (overcommitted GPU).
    return "󰘚 " + gib(stats.llm_vram) + "G" + ((stats.llm_spilled || 0) > 268435456 ? "!" : "")
  }
  readonly property string summary: {
    var gpu = stats.gpu || {}
    var vram = "Bar shows LLM GPU memory: " + gib(stats.llm_vram) + " GiB\n"
      + "GPU total in use: " + gib(gpu.vram_used) + " of " + gib(gpu.vram_total) + " GiB (incl. desktop & apps " + gib(stats.other_vram) + " GiB)"
    if (!active || !stats.primary) return "No local LLM loaded\n" + vram
    var primary = stats.primary
    return primary.runtime + (primary.model ? " · " + primary.model : "") + "\n" + (busy ? "Generating · " : "") + vram
  }

  // One collector streams a JSON snapshot every two seconds. It never starts
  // a runtime; it only reads /proc, sysfs and the runtimes' local APIs.
  Process {
    id: collector
    command: ["python3", root.scriptDir + "/llm-usage.py"]
    running: true
    stdout: SplitParser {
      onRead: function(line) {
        try {
          var data = JSON.parse(line)
          if (!data.error) root.stats = data
        } catch (e) { /* partial line during shutdown */ }
      }
    }
    onExited: restart.start()
  }
  Timer { id: restart; interval: 5000; onTriggered: collector.running = true }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
    if ("stats" in target) target.stats = root.stats
  }

  // Shape contract for shell summon/hide/toggle routing (see omarchy.weather).
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  onBarChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    // A per-load query defeats the QML component cache, so a plugin hot reload
    // also picks up a changed Panel.qml instead of an old compiled copy.
    source: Qt.resolvedUrl("Panel.qml") + "?load=" + Date.now()
    visible: false
    onLoaded: { root.injectPanel(); Qt.callLater(root.injectPanel) }
  }

  // 0..1 glow while generating; drives the chip colour.
  property real glow: 0
  SequentialAnimation on glow {
    running: root.busy
    loops: Animation.Infinite
    NumberAnimation { to: 1; duration: 650; easing.type: Easing.InOutSine }
    NumberAnimation { to: 0; duration: 650; easing.type: Easing.InOutSine }
    onRunningChanged: if (!running) root.glow = 0
  }

  Ui.WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barText
    dimmed: !root.active
    readonly property color base: root.bar ? root.bar.barForeground : Color.foreground
    foreground: root.active ? Qt.tint(Color.accent, Qt.rgba(base.r, base.g, base.b, root.glow * 0.75)) : base
    tooltipText: root.opened ? "" : root.summary
    onPressed: function(code) {
      if (code === Qt.LeftButton) root.toggle()
      else if (code === Qt.RightButton && root.bar) root.bar.run("omarchy-launch-or-focus-tui btop")
    }
  }
}
