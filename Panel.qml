pragma ComponentBehavior: Bound
import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// Detail popup: GPU memory split into "held by LLM runtimes" and "everything
// else", GPU load/temperature/power, system CPU/RAM, then one block per runtime
// with its loaded model(s), context, VRAM, CPU, RAM and connected clients.
Panel {
  id: root
  moduleName: "ubruckhaus.llm-usage"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root
  property var stats: ({})  // pushed by BarWidget on every snapshot
  readonly property var gpu: stats.gpu || ({})
  readonly property var system: stats.system || ({})
  readonly property var runtimes: stats.runtimes || []
  readonly property color fg: Color.popups.text
  readonly property color dim: Qt.darker(fg, 1.45)
  readonly property string font: root.bar ? root.bar.fontFamily : Style.font.family

  function gib(bytes) { return (Number(bytes || 0) / 1073741824).toFixed(1) }
  function pct(value) { return value === null || value === undefined ? "—" : Math.round(value) + "%" }
  function ctx(value) {
    if (!value) return ""
    return value >= 1024 ? Math.round(value / 1024) + "k ctx" : value + " ctx"
  }
  function runtimeState(runtime) {
    if (!runtime.running) return "stopped"
    var resident = runtime.models.filter(function(model) { return model.state !== "sleeping" }).length
    if (resident) return resident === 1 ? "1 model loaded" : resident + " models loaded"
    if (runtime.models.length) return "model sleeping (GPU free)"
    return "running, no model"
  }

  function open() { root.controller.show() }
  function close() { root.controller.hide() }
  function toggle() { if (root.opened) root.close(); else root.open() }

  component Meter: Item {
    id: meter
    property string label: ""
    property string value: ""
    property real fraction: 0
    property real highlight: 0     // part of the fraction drawn in the accent colour
    width: parent ? parent.width : 200
    height: meterLabel.height + Style.space(10)
    Text {
      id: meterLabel
      text: meter.label
      color: root.dim
      font.family: root.font
      font.pixelSize: Style.font.caption
    }
    Text {
      anchors.right: parent.right
      text: meter.value
      color: root.fg
      font.family: root.font
      font.pixelSize: Style.font.caption
    }
    Rectangle {
      id: track
      anchors.bottom: parent.bottom
      width: parent.width
      height: Style.space(5)
      radius: height / 2
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
      Rectangle {
        width: parent.width * Math.max(0, Math.min(1, meter.fraction))
        height: parent.height
        radius: parent.radius
        color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.45)
      }
      Rectangle {
        width: parent.width * Math.max(0, Math.min(1, meter.highlight))
        height: parent.height
        radius: parent.radius
        color: Color.accent
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: scroller
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: column
          width: scroller.width
          spacing: Style.space(10)

          PanelHero {
            foreground: root.fg
            fontFamily: root.font
            title: "LLM Monitor"
            meta: root.stats.primary
              ? root.stats.primary.runtime + (root.stats.primary.model ? " · " + root.stats.primary.model : "")
              : "No local model loaded"
            detail: root.stats.busy ? "generating" : (root.stats.active ? "idle" : "")
            iconComponent: Component {
              Text {
                text: "󰘚"
                color: root.stats.active ? Color.accent : root.dim
                font.family: root.font
                font.pixelSize: Style.font.display
              }
            }
          }

          PanelSeparator { width: parent.width; foreground: root.fg }
          PanelSectionHeader { text: "GPU MEMORY"; foreground: root.fg; fontFamily: root.font }

          // Total in use, then one stacked bar split into LLM / desktop & apps / free,
          // with a legend. The bar icon shows only the LLM part, and says so here.
          Item {
            width: parent.width
            height: totalText.height
            Text {
              id: totalText
              text: root.gib(root.gpu.vram_used) + " of " + root.gib(root.gpu.vram_total) + " GiB in use"
              color: root.fg
              font.family: root.font
              font.pixelSize: Style.font.body
              font.bold: true
            }
            Text {
              anchors.right: parent.right
              anchors.baseline: totalText.baseline
              text: root.gib(Math.max(0, (root.gpu.vram_total || 0) - (root.gpu.vram_used || 0))) + " GiB free"
              color: root.dim
              font.family: root.font
              font.pixelSize: Style.font.caption
            }
          }

          Rectangle {
            id: stack
            width: parent.width
            height: Style.space(10)
            radius: height / 2
            clip: true
            color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
            readonly property real total: root.gpu.vram_total || 1
            Row {
              anchors.fill: parent
              Rectangle {
                width: stack.width * Math.min(1, (root.stats.llm_vram || 0) / stack.total)
                height: parent.height
                color: Color.accent
              }
              Rectangle {
                width: stack.width * Math.min(1, (root.stats.other_vram || 0) / stack.total)
                height: parent.height
                color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.5)
              }
            }
          }

          component LegendRow: Item {
            id: legend
            property color swatch: "transparent"
            property bool outlined: false
            property string name: ""
            property string amount: ""
            property string note: ""
            width: parent ? parent.width : 300
            height: Math.max(legendName.height, legendNote.visible ? legendName.height + legendNote.height : 0)
            Rectangle {
              id: swatchBox
              y: (legendName.height - height) / 2
              width: Style.space(10); height: width; radius: Style.space(2)
              color: legend.outlined ? "transparent" : legend.swatch
              border.width: legend.outlined ? 1 : 0
              border.color: root.dim
            }
            Text {
              id: legendName
              anchors.left: swatchBox.right
              anchors.leftMargin: Style.space(8)
              text: legend.name
              color: root.fg
              font.family: root.font
              font.pixelSize: Style.font.caption
            }
            Text {
              anchors.right: parent.right
              text: legend.amount
              color: root.fg
              font.family: root.font
              font.pixelSize: Style.font.caption
            }
            Text {
              id: legendNote
              visible: legend.note !== ""
              anchors.top: legendName.bottom
              anchors.left: legendName.left
              anchors.right: parent.right
              elide: Text.ElideRight
              text: legend.note
              color: root.dim
              font.family: root.font
              font.pixelSize: Style.font.caption
            }
          }

          Column {
            width: parent.width
            spacing: Style.space(4)
            LegendRow {
              swatch: Color.accent
              name: "LLM models"
              amount: root.gib(root.stats.llm_vram) + " GiB"
              note: "↑ this number is shown in the bar"
            }
            LegendRow {
              swatch: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.5)
              name: "Desktop & apps"
              amount: root.gib(root.stats.other_vram) + " GiB"
              note: (root.stats.other_apps || []).length
                ? (root.stats.other_apps || []).map(function(app) { return app.name + " " + root.gib(app.vram) }).join(" · ")
                : ""
            }
            LegendRow {
              outlined: true
              name: "Free"
              amount: root.gib(Math.max(0, (root.gpu.vram_total || 0) - (root.gpu.vram_used || 0))) + " GiB"
            }
          }

          Text {
            visible: (root.stats.llm_spilled || 0) > 268435456
            width: parent.width
            wrapMode: Text.WordWrap
            text: "⚠ " + root.gib(root.stats.llm_spilled) + " GiB of model memory does not fit in VRAM and was moved to system RAM. "
              + "Generation is much slower; unload other models or free GPU memory."
            color: Color.urgent
            font.family: root.font
            font.pixelSize: Style.font.caption
          }
          PanelSectionHeader { text: "GPU ACTIVITY"; foreground: root.fg; fontFamily: root.font }
          Meter {
            label: "Load"
            value: root.pct(root.gpu.busy)
              + (root.gpu.temp ? "  ·  " + Math.round(root.gpu.temp) + " °C" : "")
              + (root.gpu.power ? "  ·  " + Math.round(root.gpu.power) + " W" : "")
            fraction: (root.gpu.busy || 0) / 100
          }

          PanelSectionHeader { text: "SYSTEM"; foreground: root.fg; fontFamily: root.font }
          Meter {
            label: "CPU"
            value: root.pct(root.system.cpu)
            fraction: (root.system.cpu || 0) / 100
          }
          Meter {
            label: "RAM"
            value: root.gib(root.system.mem_used) + " / " + root.gib(root.system.mem_total) + " GiB"
            fraction: root.system.mem_total ? root.system.mem_used / root.system.mem_total : 0
          }

          PanelSeparator { width: parent.width; foreground: root.fg }
          PanelSectionHeader { text: "RUNTIMES"; foreground: root.fg; fontFamily: root.font }

          Repeater {
            model: root.runtimes
            delegate: Column {
              id: block
              required property var modelData
              width: column.width
              spacing: Style.space(3)
              readonly property bool loaded: modelData.models.some(function(model) { return model.state !== "sleeping" })

              Item {
                width: parent.width
                height: runtimeName.height
                Rectangle {
                  id: dot
                  anchors.verticalCenter: parent.verticalCenter
                  width: Style.space(8); height: width; radius: width / 2
                  color: block.loaded ? Color.accent : (block.modelData.running ? root.dim : "transparent")
                  border.width: 1
                  border.color: block.loaded ? Color.accent : root.dim
                }
                Text {
                  id: runtimeName
                  anchors.left: dot.right
                  anchors.leftMargin: Style.space(8)
                  text: block.modelData.name
                  color: root.fg
                  font.family: root.font
                  font.pixelSize: Style.font.body
                  font.bold: block.loaded
                }
                Text {
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  text: root.runtimeState(block.modelData)
                  color: block.loaded ? Color.accent : root.dim
                  font.family: root.font
                  font.pixelSize: Style.font.caption
                }
              }

              Repeater {
                model: block.modelData.models
                delegate: Text {
                  required property var modelData
                  width: block.width
                  leftPadding: Style.space(16)
                  elide: Text.ElideMiddle
                  text: "󰗚 " + modelData.name
                    + (modelData.context ? "  ·  " + root.ctx(modelData.context) : "")
                    + (modelData.state && modelData.state !== "loaded" ? "  ·  " + modelData.state : "")
                  color: root.fg
                  font.family: root.font
                  font.pixelSize: Style.font.caption
                }
              }

              Text {
                visible: block.modelData.running
                width: block.width
                leftPadding: Style.space(16)
                wrapMode: Text.WordWrap
                text: "VRAM " + root.gib(block.modelData.vram) + " GiB"
                  + ((block.modelData.spilled || 0) > 268435456 ? " (+" + root.gib(block.modelData.spilled) + " GiB in RAM)" : "")
                  + "  ·  CPU " + root.pct(block.modelData.cpu)
                  + "  ·  RAM " + root.gib(block.modelData.memory) + " GiB"
                  + (block.modelData.clients.length ? "  ·  used by " + block.modelData.clients.join(", ") : "")
                color: root.dim
                font.family: root.font
                font.pixelSize: Style.font.caption
              }
            }
          }

          PanelSeparator { width: parent.width; foreground: root.fg }

          Row {
            spacing: Style.space(8)
            Button {
              text: "Free GPU memory"
              iconText: "󰆴"
              bordered: true
              foreground: root.fg
              fontFamily: root.font
              enabled: root.stats.active === true
              tooltipText: "Stop llama-server and Ollama, unload LM Studio models"
              onClicked: Quickshell.execDetached(["bash", root.hostWidget.scriptDir + "/free-gpu.sh"])
            }
            Button {
              text: "btop"
              iconText: "󰍛"
              bordered: true
              foreground: root.fg
              fontFamily: root.font
              onClicked: { root.close(); if (root.bar) root.bar.run("omarchy-launch-or-focus-tui btop") }
            }
          }
        }
      }
    }
  }
}
