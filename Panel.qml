import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

Panel {
  id: root
  moduleName: "matheoatche.aircast"
  manageIpc: false
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Injected by the shell; __sourceDir is how plugins locate bundled scripts.
  property var manifest: null
  readonly property string cli: manifest && manifest.__sourceDir
    ? manifest.__sourceDir + "/bin/aircast"
    : decodeURIComponent(Qt.resolvedUrl("bin/aircast").toString().replace(/^(file|qs):\/\//, ""))
  readonly property string castIcon: "󰄘"
  readonly property string castingIcon: "󰄙"

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  property var status: ({ active: false })
  property var devices: []
  property var sources: ({ monitors: [], workspaces: [], windows: [] })
  property var doctor: ({ missing: [], missingOptional: [], firewallOpen: true })
  property string deviceId: ""
  property string sourceId: ""
  property string errorText: ""
  property int cursor: -1
  // The source picker is a sub-view, so Start stays in reach however many
  // workspaces and apps there are.
  property bool picking: false
  property string query: ""

  readonly property bool casting: status.active === true
  readonly property bool scanning: devicesProc.running
  readonly property bool busy: actionProc.running
  readonly property bool virtualDisplayReady: (doctor.missingOptional || []).indexOf("wf-recorder") === -1
  readonly property bool airplayTvFound: devices.some(function(d) { return d.protocol === "airplay" && d.video !== false })
  readonly property bool airplaySpeakerFound: devices.some(function(d) { return d.protocol === "airplay" && d.video === false })
  readonly property bool pyatvMissing: airplayTvFound && (doctor.missingOptional || []).indexOf("atvremote") !== -1
  readonly property bool airplayAudioMissing: airplaySpeakerFound && doctor.airplayAudio === false
  readonly property var device: deviceById(deviceId)
  // Speakers can only take audio, so screen sources are hidden for them.
  readonly property bool speakerSelected: device !== null && device.video === false
  readonly property var sourceOptions: buildSourceOptions()
  readonly property var chosenSource: optionByValue(sourceId)
  readonly property var rows: buildRows()

  // Settings from shell.json, handed to the CLI as environment.
  function castEnv() {
    return [
      "AIRCAST_RESOLUTION=" + setting("resolution", "1920x1080"),
      "AIRCAST_FPS=" + setting("fps", 30),
      "AIRCAST_AUDIO=" + (setting("audio", true) ? "yes" : "no"),
      "AIRCAST_PORT=" + setting("port", 8765),
      "AIRCAST_LOW_LATENCY=" + (setting("lowLatency", true) ? "yes" : "no")
    ]
  }

  function run(proc, args) {
    proc.command = ["env"].concat(castEnv(), [cli], args)
    proc.running = true
  }

  function parse(text, fallback) {
    try { return JSON.parse(text) } catch (e) { return fallback }
  }

  function refreshStatus() { if (!statusProc.running) run(statusProc, ["status"]) }
  function refreshAll() {
    refreshStatus()
    if (!sourcesProc.running) run(sourcesProc, ["sources"])
    if (!doctorProc.running) run(doctorProc, ["doctor"])
    if (!devicesProc.running) run(devicesProc, ["devices"])
  }

  function deviceById(id) {
    for (var i = 0; i < devices.length; i++) if (devices[i].id === id) return devices[i]
    return null
  }

  onSpeakerSelectedChanged: if (speakerSelected) sourceId = "audio"

  function buildRows() {
    var list = []
    function header(title) { list.push({ kind: "header", value: "", icon: "", title: title, detail: "", enabled: false }) }
    function row(kind, value, icon, title, detail, enabled) {
      list.push({ kind: kind, value: value, icon: icon, title: title, detail: detail || "", enabled: enabled !== false })
    }

    if (casting) {
      header("NOW CASTING")
      row("stop", "", "󰓛", "Stop casting", "")
      return list
    }
    if (picking) return buildPickerRows(list, header, row)

    if (doctor.firewallOpen === false || (doctor.missing || []).length > 0 || !virtualDisplayReady || pyatvMissing || airplayAudioMissing || doctor.intelDriverMissing === true) {
      header("SETUP")
      if ((doctor.missing || []).length > 0)
        row("info", "", "󰀦", "Missing: " + doctor.missing.join(", "), "omarchy pkg add …", false)
      if (doctor.firewallOpen === false)
        row("firewall", "", "󰒃", "Open firewall for casting", "needs sudo")
      if (doctor.intelDriverMissing === true)
        row("install", "", "󰇚", "Install Intel video driver", "less delay & CPU")
      if (!virtualDisplayReady)
        row("install", "", "󰇚", "Install wf-recorder", "for workspaces & apps")
      if (pyatvMissing)
        row("install", "", "󰇚", "Install pyatv", "for Apple TVs")
      if (airplayAudioMissing)
        row("install", "", "󰇚", "Install AirPlay audio", "for speakers")
    }

    header("RECEIVERS")
    if (devices.length === 0)
      row("info", "", scanning ? "󰑓" : "󰅛", scanning ? "Searching the network…" : "No receivers found", scanning ? "" : "r to search again", false)
    for (var d = 0; d < devices.length; d++) {
      var device = devices[d]
      row("device", device.id, device.video === false ? "󰜟" : (device.protocol === "airplay" ? "󰀵" : "󰔂"), device.name,
          (device.protocol === "airplay" ? "AirPlay" : "Chromecast") + (device.video === false ? " speaker" : ""))
    }

    header("CAST")
    var src = chosenSource
    row("pick", "", src ? src.icon : "󰐕", src ? src.title : "Choose what to cast…",
        speakerSelected ? "speakers play audio only" : (src ? src.detail : "/ to search"), !speakerSelected)

    header("")
    var ready = deviceId !== "" && src !== null
    row("start", "", "󰐊", busy ? "Connecting…" : "Start casting",
        ready ? "" : (deviceId === "" ? "Pick a receiver" : "Pick what to cast"), ready && !busy)
    return list
  }

  // The picker: grouped while the search is empty, ranked by match once typing.
  function buildPickerRows(list, header, row) {
    var q = query.trim()
    if (q === "") {
      var group = null
      for (var i = 0; i < sourceOptions.length; i++) {
        var o = sourceOptions[i]
        if (o.group !== group) header(group = o.group)
        row(o.kind, o.value, o.icon, o.title, o.detail)
      }
      return list
    }

    var ranked = []
    for (var j = 0; j < sourceOptions.length; j++) {
      var score = matchScore(sourceOptions[j], q)
      if (score >= 0) ranked.push({ option: sourceOptions[j], score: score })
    }
    ranked.sort(function(a, b) { return b.score - a.score })
    if (ranked.length === 0) row("info", "", "󰍉", "Nothing matches “" + q + "”", "", false)
    for (var k = 0; k < ranked.length; k++) {
      var r = ranked[k].option
      row(r.kind, r.value, r.icon, r.title, r.detail)
    }
    return list
  }

  function buildSourceOptions() {
    var list = []
    function add(group, kind, value, icon, title, detail) {
      list.push({ group: group, kind: kind, value: value, icon: icon, title: title, detail: detail })
    }

    add("AUDIO", "source", "audio", "󰎇", "Audio only", "laptop goes quiet")
    if (speakerSelected) return list

    for (var m = 0; m < sources.monitors.length; m++) {
      var monitor = sources.monitors[m]
      add("SCREEN", "source", "screen:" + monitor.name, "󰍹",
          sources.monitors.length > 1 ? "Screen " + monitor.name : "Entire screen",
          monitor.width + "×" + monitor.height)
    }
    add("SCREEN", "source", "region", "󰩭", "Select a region…", "Drag after starting")

    // Without wf-recorder these rows offer the install instead of a source.
    var vdKind = virtualDisplayReady ? "source" : "install"
    var vdNote = virtualDisplayReady ? "" : "install wf-recorder"
    // With several monitors, say which one each workspace is on (also searchable).
    var multiMonitor = sources.monitors.length > 1
    for (var w = 0; w < sources.workspaces.length; w++) {
      var ws = sources.workspaces[w]
      add("WORKSPACE · MOVES TO THE TV WHILE CASTING", vdKind, "workspace:" + ws.id, "󰕰", "Workspace " + ws.name,
          vdNote || (ws.windows + (ws.windows === 1 ? " window" : " windows") + (multiMonitor ? " · " + ws.monitor : "")))
    }
    for (var a = 0; a < sources.windows.length; a++) {
      var win = sources.windows[a]
      add("APP · MOVES TO THE TV WHILE CASTING", vdKind, "window:" + win.address, "󰖯", win.title || win["class"],
          vdNote || (win["class"] + " · ws " + win.workspace))
    }
    return list
  }

  function optionByValue(value) {
    for (var i = 0; i < sourceOptions.length; i++)
      if (sourceOptions[i].kind === "source" && sourceOptions[i].value === value) return sourceOptions[i]
    return null
  }

  // A substring scores highest, earlier and on a word start better; otherwise
  // the letters must appear in order, with runs and word starts scoring more.
  function fuzzyScore(text, term) {
    var t = text.toLowerCase()
    var at = t.indexOf(term)
    if (at >= 0) return 100 + term.length * 4 - at + (at === 0 || /[^a-z0-9]/.test(t.charAt(at - 1)) ? 20 : 0)
    var score = 0, from = 0, last = -2
    for (var i = 0; i < term.length; i++) {
      var j = t.indexOf(term.charAt(i), from)
      if (j < 0) return -1
      score += j === last + 1 ? 3 : (j === 0 || /[^a-z0-9]/.test(t.charAt(j - 1)) ? 2 : 0)
      last = j
      from = j + 1
    }
    return score
  }

  // Every word of the query has to match; hits in the title count extra.
  function matchScore(option, query) {
    var terms = query.toLowerCase().split(/\s+/)
    var haystack = option.title + " " + option.detail + " " + option.group
    var total = 0
    for (var i = 0; i < terms.length; i++) {
      if (terms[i] === "") continue
      var inTitle = fuzzyScore(option.title, terms[i])
      var score = Math.max(inTitle >= 0 ? inTitle + 10 : -1, fuzzyScore(haystack, terms[i]))
      if (score < 0) return -1
      total += score
    }
    return total
  }

  function selectable(index) {
    var r = rows[index]
    return r && r.kind !== "header" && r.kind !== "info"
  }

  function firstSelectable() {
    for (var i = 0; i < rows.length; i++) if (selectable(i)) return i
    return -1
  }

  function openPicker() {
    if (speakerSelected) return
    picking = true
    searchField.text = ""
    errorText = ""
    flick.contentY = 0
    Qt.callLater(function() {
      var chosen = -1
      for (var i = 0; i < root.rows.length; i++) if (root.isSelected(root.rows[i])) chosen = i
      root.cursor = chosen >= 0 ? chosen : root.firstSelectable()
      searchField.forceActiveFocus()
    })
  }

  function closePicker() {
    picking = false
    flick.contentY = 0
    Qt.callLater(function() {
      root.cursor = root.indexOfKind("pick")
      keys.forceActiveFocus()
    })
  }

  // Keep the keyboard cursor in view while it walks a long list.
  function ensureCursorVisible() {
    var item = rowRepeater.itemAt(cursor)
    if (!item || flick.height <= 0) return
    if (cursor === firstSelectable()) { flick.contentY = 0; return }
    var y = item.mapToItem(content, 0, 0).y
    if (y < flick.contentY) flick.contentY = y
    else if (y + item.height > flick.contentY + flick.height)
      flick.contentY = Math.min(y + item.height - flick.height, flick.contentHeight - flick.height)
  }

  function moveCursor(dy) {
    var i = cursor
    for (var n = 0; n < rows.length; n++) {
      i = (i + dy + rows.length) % rows.length
      if (selectable(i)) { cursor = i; return }
    }
  }

  function indexOfKind(kind) {
    for (var i = 0; i < rows.length; i++) if (rows[i].kind === kind) return i
    return -1
  }

  function activate(index) {
    var r = rows[index]
    if (!r || !selectable(index) || !r.enabled) return
    cursor = index
    errorText = ""
    if (r.kind === "device") deviceId = r.value
    else if (r.kind === "pick") openPicker()
    else if (r.kind === "source") {
      sourceId = r.value
      closePicker()
      Qt.callLater(function() { root.cursor = root.indexOfKind(root.deviceId !== "" ? "start" : "device") })
    }
    else if (r.kind === "start") startCast()
    else if (r.kind === "stop") stopCast()
    else if (r.kind === "firewall" || r.kind === "install") {
      // Both need sudo, so they run in a terminal; the panel rechecks on next open.
      Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", "'" + cli + "' " + r.kind])
      root.close()
    }
  }

  function startCast() {
    if (busy || deviceId === "" || sourceId === "") return
    // Region picking happens on screen, so get the panel out of the way first.
    if (sourceId === "region") root.close()
    run(actionProc, ["start", deviceId, sourceId])
  }

  function stopCast() {
    if (!busy) run(actionProc, ["stop"])
  }

  function isSelected(r) {
    return (r.kind === "device" && r.value === deviceId) || (r.kind === "source" && r.value === sourceId)
  }

  Component.onCompleted: refreshStatus()
  onOpenedChanged: if (opened) {
    picking = false
    cursor = -1
    errorText = status.error || ""
    refreshAll()
    Qt.callLater(function() { keys.forceActiveFocus() })
  }
  onRowsChanged: if (cursor >= rows.length) cursor = -1
  onCursorChanged: Qt.callLater(ensureCursorVisible)
  onCastingChanged: if (casting) picking = false
  onQueryChanged: if (picking) Qt.callLater(function() { root.cursor = root.firstSelectable() })

  Timer {
    interval: root.opened || root.casting ? 2000 : 10000
    running: true
    repeat: true
    onTriggered: root.refreshStatus()
  }

  Process {
    id: statusProc
    stdout: StdioCollector {
      onStreamFinished: {
        root.status = root.parse(text, { active: false })
        if (root.deviceId === "" && root.status.lastDevice) root.deviceId = root.status.lastDevice
      }
    }
  }

  Process {
    id: devicesProc
    stdout: StdioCollector {
      onStreamFinished: {
        root.devices = root.parse(text, [])
        if (root.deviceId === "" && root.devices.length === 1) root.deviceId = root.devices[0].id
      }
    }
  }

  Process {
    id: sourcesProc
    stdout: StdioCollector {
      onStreamFinished: root.sources = root.parse(text, { monitors: [], workspaces: [], windows: [] })
    }
  }

  Process {
    id: doctorProc
    stdout: StdioCollector {
      onStreamFinished: root.doctor = root.parse(text, root.doctor)
    }
  }

  Process {
    id: actionProc
    stderr: StdioCollector {
      onStreamFinished: root.errorText = text.trim().replace(/^aircast: /gm, "")
    }
    onExited: function(code) {
      root.refreshStatus()
      if (code === 0 && root.opened) root.close()
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.casting ? root.castingIcon : root.castIcon
    foreground: root.casting ? root.barForeground : Qt.darker(root.barForeground, 1.55)
    tooltipText: root.casting
      ? "Casting " + root.status.sourceLabel + " to " + root.status.device.name + " · right-click to stop"
      : "Cast to a TV"
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton && root.casting) root.stopCast()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: popup
    owner: root
    anchorItem: button
    bar: root.bar
    open: root.opened
    focusTarget: keys
    contentWidth: popup.fittedContentWidth(Style.space(380))
    contentHeight: popup.fittedContentHeight(content.implicitHeight + searchBar.height, Style.space(560))

    PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      blocked: searchField.activeFocus
      onMoveRequested: function(dx, dy) {
        if (dy !== 0) root.moveCursor(dy)
        else if (dx > 0 && !root.picking && root.rows[root.cursor] && root.rows[root.cursor].kind === "pick") root.openPicker()
        else if (dx < 0 && root.picking) root.closePicker()
      }
      onActivateRequested: root.activate(root.cursor)
      onCloseRequested: root.picking ? root.closePicker() : root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refreshAll()
        else if (t === "s" || t === "S") root.casting ? root.stopCast() : root.startCast()
        else if (t === "/" && !root.casting) root.openPicker()
      }

      // Pinned above the list so the search stays put while results scroll.
      Item {
        id: searchBar
        visible: root.picking
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: visible ? searchRow.implicitHeight + Style.space(8) : 0

        RowLayout {
          id: searchRow
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.space(8)

          Text {
            text: "󰁍"
            color: backArea.containsMouse ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            Layout.preferredWidth: Style.space(20)
            horizontalAlignment: Text.AlignHCenter

            MouseArea {
              id: backArea
              anchors.fill: parent
              anchors.margins: -Style.space(4)
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.closePicker()
            }
          }

          TextField {
            id: searchField
            Layout.fillWidth: true
            placeholderText: "Search screens, workspaces, apps…"
            foreground: root.foreground
            font.family: root.fontFamily
            onTextChanged: root.query = text

            Keys.onPressed: function(event) {
              var ctrl = event.modifiers & Qt.ControlModifier
              if (event.key === Qt.Key_Down || (ctrl && (event.key === Qt.Key_N || event.key === Qt.Key_J))) {
                root.moveCursor(1); event.accepted = true
              } else if (event.key === Qt.Key_Up || (ctrl && (event.key === Qt.Key_P || event.key === Qt.Key_K))) {
                root.moveCursor(-1); event.accepted = true
              } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                root.activate(root.cursor); event.accepted = true
              } else if (event.key === Qt.Key_Escape) {
                if (text !== "") text = ""
                else root.closePicker()
                event.accepted = true
              } else if (event.key === Qt.Key_Backspace && text === "") {
                root.closePicker(); event.accepted = true
              } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
                event.accepted = true
              }
            }
          }
        }
      }

      Flickable {
        id: flick
        anchors.top: searchBar.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: content
          width: flick.width
          spacing: Style.space(6)

          PanelHero {
            visible: !root.picking
            iconComponent: Component {
              Text {
                text: root.casting ? root.castingIcon : root.castIcon
                color: root.casting ? root.foreground : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            title: root.casting ? root.status.device.name : "Cast"
            meta: root.casting
              ? (root.status.phase === "casting" ? root.status.sourceLabel : "Connecting · " + root.status.sourceLabel)
              : "Chromecast · AirPlay"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Text {
            width: parent.width
            visible: root.errorText !== ""
            text: root.errorText
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Repeater {
            id: rowRepeater
            model: root.rows

            delegate: Item {
              id: rowItem
              required property var modelData
              required property int index
              width: content.width
              implicitHeight: modelData.kind === "header"
                ? (modelData.title === "" ? Style.space(4) : sectionHeader.implicitHeight + Style.space(8))
                : surface.implicitHeight

              PanelSectionHeader {
                id: sectionHeader
                visible: rowItem.modelData.kind === "header" && rowItem.modelData.title !== ""
                anchors.bottom: parent.bottom
                text: rowItem.modelData.title
                foreground: root.foreground
                fontFamily: root.fontFamily
              }

              CursorSurface {
                id: surface
                visible: rowItem.modelData.kind !== "header"
                width: parent.width
                implicitHeight: line.implicitHeight + Style.spacing.xl
                hasCursor: root.cursor === rowItem.index
                current: root.isSelected(rowItem.modelData)
                foreground: root.foreground
                opacity: rowItem.modelData.enabled ? 1 : 0.55

                MouseArea {
                  anchors.fill: parent
                  hoverEnabled: true
                  enabled: root.selectable(rowItem.index)
                  cursorShape: rowItem.modelData.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                  onEntered: root.cursor = rowItem.index
                  onClicked: root.activate(rowItem.index)
                }

                RowLayout {
                  id: line
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.leftMargin: Style.space(8)
                  anchors.rightMargin: Style.space(8)
                  spacing: Style.space(8)

                  Text {
                    text: rowItem.modelData.icon
                    color: root.isSelected(rowItem.modelData) || ["start", "stop", "pick"].indexOf(rowItem.modelData.kind) !== -1 ? root.foreground : root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    Layout.preferredWidth: Style.space(20)
                    horizontalAlignment: Text.AlignHCenter
                  }

                  Text {
                    Layout.fillWidth: true
                    text: rowItem.modelData.title
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: rowItem.modelData.kind === "start" || rowItem.modelData.kind === "stop"
                  }

                  Text {
                    visible: text !== ""
                    Layout.maximumWidth: content.width * 0.45
                    text: rowItem.modelData.detail
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    visible: rowItem.modelData.kind === "pick" && rowItem.modelData.enabled
                    text: "󰅂"
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
