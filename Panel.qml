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
  // Same green / yellow / orange / red levels as the bar chip.
  function levelColor(fraction) {
    return root.hostWidget && root.hostWidget.levelColor ? root.hostWidget.levelColor(fraction) : Color.accent
  }
  function rate(value) { return value === null || value === undefined ? "—" : value.toFixed(value >= 100 ? 0 : 1) }
  readonly property var genSpeed: (root.stats.primary || {}).speed || null
  function tokens(value) {
    value = Number(value || 0)
    return value >= 1000 ? (value / 1000).toFixed(value >= 10000 ? 0 : 1) + "k" : String(value)
  }
  function mib(bytes) {
    bytes = Number(bytes || 0)
    return bytes >= 1073741824 ? root.gib(bytes) + " GiB" : Math.round(bytes / 1048576) + " MiB"
  }
  function runtimeState(runtime) {
    if (!runtime.running) return runtime.installed === false ? "not installed" : "stopped"
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
      textFormat: Text.PlainText
      id: meterLabel
      text: meter.label
      color: root.dim
      font.family: root.font
      font.pixelSize: Style.font.caption
    }
    Text {
      textFormat: Text.PlainText
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
                textFormat: Text.PlainText
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
              textFormat: Text.PlainText
              id: totalText
              text: root.gib(root.gpu.vram_used) + " of " + root.gib(root.gpu.vram_total) + " GiB in use"
              color: root.fg
              font.family: root.font
              font.pixelSize: Style.font.body
              font.bold: true
            }
            Text {
              textFormat: Text.PlainText
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
            property real fraction: -1    // >= 0 draws a fill bar in the level colour
            width: parent ? parent.width : 300
            height: Math.max(legendName.height, legendNote.visible ? legendName.height + legendNote.height : 0)
              + (fraction >= 0 ? Style.space(8) : 0)
            Rectangle {
              id: swatchBox
              y: (legendName.height - height) / 2
              width: Style.space(10); height: width; radius: Style.space(2)
              color: legend.outlined ? "transparent" : legend.swatch
              border.width: legend.outlined ? 1 : 0
              border.color: root.dim
            }
            Text {
              textFormat: Text.PlainText
              id: legendName
              anchors.left: swatchBox.right
              anchors.leftMargin: Style.space(8)
              text: legend.name
              color: root.fg
              font.family: root.font
              font.pixelSize: Style.font.caption
            }
            Text {
              textFormat: Text.PlainText
              anchors.right: parent.right
              text: legend.amount
              color: root.fg
              font.family: root.font
              font.pixelSize: Style.font.caption
            }
            Text {
              textFormat: Text.PlainText
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
            Rectangle {
              visible: legend.fraction >= 0
              anchors.bottom: parent.bottom
              anchors.left: legendName.left
              anchors.right: parent.right
              height: Style.space(4)
              radius: height / 2
              color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
              Rectangle {
                width: parent.width * Math.max(0, Math.min(1, legend.fraction))
                height: parent.height
                radius: parent.radius
                color: root.levelColor(legend.fraction)
                Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
                Behavior on color { ColorAnimation { duration: 400 } }
              }
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
            // The two buffers that fill while you work: the context (whole conversation)
            // and the reply being generated. Memory is the KV cache those tokens occupy.
            LegendRow {
              readonly property var p: root.stats.primary || ({})
              readonly property real filled: p.context ? (p.context_used || 0) / p.context : 0
              visible: !!p.context && p.context_used !== null && p.context_used !== undefined
              swatch: root.levelColor(filled)
              name: "Context buffer"
              amount: p.context_memory
                ? "≈ " + root.mib(p.context_memory * filled) + " of " + root.mib(p.context_memory)
                : Math.round(filled * 100) + "%"
              note: root.tokens(p.context_used) + " / " + root.tokens(p.context) + " tokens (" + Math.round(filled * 100) + "%)"
              fraction: filled
            }
            LegendRow {
              readonly property var p: root.stats.primary || ({})
              readonly property real filled: p.output_limit ? (p.output_used || 0) / p.output_limit : 0
              visible: !!p.output_limit
              swatch: root.levelColor(filled)
              name: "Output buffer"
              amount: p.context_memory && p.context
                ? "≈ " + root.mib(p.context_memory * (p.output_used || 0) / p.context)
                : Math.round(filled * 100) + "%"
              note: (p.generating ? "generating " : "last reply ") + root.tokens(p.output_used) + " / "
                + root.tokens(p.output_limit) + " tokens (" + Math.round(filled * 100) + "%)"
              fraction: filled
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
            textFormat: Text.PlainText
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

          // Generation speed of the loaded model: live while it writes, plus
          // min / avg / max over the replies since the monitor started watching it.
          Meter {
            visible: root.genSpeed !== null
            label: "Speed" + (!root.genSpeed ? ""
              : root.genSpeed.count
                ? "  ·  min " + root.rate(root.genSpeed.min) + "  avg " + root.rate(root.genSpeed.avg) + "  max " + root.rate(root.genSpeed.max)
                : root.genSpeed.avg ? "  ·  avg " + root.rate(root.genSpeed.avg) + " since load" : "")
            value: root.genSpeed && root.genSpeed.now !== null && root.genSpeed.now !== undefined
              ? root.rate(root.genSpeed.now) + " tok/s"
              : (root.genSpeed && root.genSpeed.avg ? "idle" : "")
            fraction: root.genSpeed && root.genSpeed.now ? root.genSpeed.now / Math.max(root.genSpeed.now, root.genSpeed.max || 0) : 0
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
                  textFormat: Text.PlainText
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
                  textFormat: Text.PlainText
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
                delegate: Column {
                  id: modelBlock
                  required property var modelData
                  readonly property bool hasUsage: modelData.context_used !== undefined && modelData.context_used !== null && modelData.context > 0
                  width: block.width
                  spacing: Style.space(3)
                  Text {
                    textFormat: Text.PlainText
                    width: parent.width
                    leftPadding: Style.space(16)
                    elide: Text.ElideMiddle
                    text: "󰗚 " + modelBlock.modelData.name
                      + (modelBlock.modelData.context && !modelBlock.hasUsage ? "  ·  " + root.ctx(modelBlock.modelData.context) : "")
                      + (modelBlock.modelData.state && modelBlock.modelData.state !== "loaded" ? "  ·  " + modelBlock.modelData.state : "")
                    color: root.fg
                    font.family: root.font
                    font.pixelSize: Style.font.caption
                  }
                  // Tokens in the context window, and the VRAM llama.cpp reserved for it (KV cache).
                  Item {
                    visible: modelBlock.hasUsage
                    x: Style.space(16)
                    width: parent.width - x
                    height: visible ? Math.max(contextLabel.height, contextValue.height) + Style.space(8) : 0
                    readonly property real fraction: modelBlock.hasUsage ? modelBlock.modelData.context_used / modelBlock.modelData.context : 0
                    Text {
                      textFormat: Text.PlainText
                      id: contextLabel
                      text: "Context"
                        + (modelBlock.modelData.context_memory ? "  ·  " + root.mib(modelBlock.modelData.context_memory) + " VRAM" : "")
                      color: root.dim
                      font.family: root.font
                      font.pixelSize: Style.font.caption
                    }
                    Text {
                      textFormat: Text.PlainText
                      id: contextValue
                      anchors.right: parent.right
                      text: root.tokens(modelBlock.modelData.context_used) + " / " + root.tokens(modelBlock.modelData.context)
                        + " tokens (" + Math.round(parent.fraction * 100) + "%)"
                      color: parent.fraction >= 0.9 ? root.levelColor(parent.fraction) : root.fg
                      font.family: root.font
                      font.pixelSize: Style.font.caption
                    }
                    Rectangle {
                      anchors.bottom: parent.bottom
                      width: parent.width
                      height: Style.space(4)
                      radius: height / 2
                      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
                      Rectangle {
                        width: parent.width * Math.max(0, Math.min(1, parent.parent.fraction))
                        height: parent.height
                        radius: parent.radius
                        color: root.levelColor(parent.parent.fraction)
                        Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
                      }
                    }
                  }
                }
              }

              Text {
                textFormat: Text.PlainText
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
              tooltipText: "Stop llama-server, Ollama and vLLM services, unload LM Studio models"
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
