const COMMAND_LABELS = {
  a: "lever",
  s: "left stop",
  d: "middle stop",
  f: "right stop",
  g: "max bet",
};

const connectionStatus = document.querySelector("#connection-status");
const controlStatus = document.querySelector("#control-status");
const timeoutStatus = document.querySelector("#timeout-status");
const serialStatus = document.querySelector("#serial-status");
const obsStatus = document.querySelector("#obs-status");
const videoStatus = document.querySelector("#video-status");
const videoFrameHost = document.querySelector("#video-frame-host");
const takeControlButton = document.querySelector("#take-control");
const quitButton = document.querySelector("#quit-control");
const controlButtons = [...document.querySelectorAll("[data-command]")];
const events = document.querySelector("#events");

let socket;
let canControl = false;
let reconnectTimer;

function connect() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

  socket.addEventListener("open", () => {
    connectionStatus.textContent = "Connected";
    addEvent("Connected to local server");
  });

  socket.addEventListener("message", (event) => {
    handleMessage(JSON.parse(event.data));
  });

  socket.addEventListener("close", () => {
    canControl = false;
    updateControls();
    connectionStatus.textContent = "Disconnected, retrying...";
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 1200);
  });
}

function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message));
  }
}

function handleMessage(message) {
  if (message.type === "state") {
    canControl = message.can_control;
    controlStatus.textContent = message.status_text;
    timeoutStatus.textContent = message.seconds_until_timeout === null
      ? "--"
      : `${message.seconds_until_timeout}s`;
    serialStatus.textContent = message.serial.open
      ? `${message.serial.port} open at ${message.serial.baud_rate}`
      : `Serial unavailable: ${message.serial.last_error || "not open"}`;
    obsStatus.textContent = formatObsStatus(message.obs);
    updateVideo(message);
    updateControls();
    return;
  }

  if (message.type === "control_granted") {
    addEvent("Control granted");
  } else if (message.type === "control_denied") {
    addEvent(`Control denied: ${message.reason}`);
  } else if (message.type === "control_released") {
    addEvent(`Control released (${message.reason})`);
  } else if (message.type === "cooldown_blocked") {
    addEvent(`${message.label} blocked for ${message.remaining_seconds}s`);
  } else if (message.type === "serial_error") {
    addEvent(`Serial error: ${message.message}`);
  } else if (message.type === "obs_error") {
    addEvent(`OBS error: ${message.message}`);
  } else if (message.type === "command_sent") {
    addEvent(`${message.command.toUpperCase()} ${message.label} sent`);
    pulseButton(message.command);
  } else if (message.type === "timeout_expired") {
    addEvent("Control timed out");
  } else if (message.type === "connected") {
    addEvent("Spectator mode ready");
  } else if (message.type === "error") {
    addEvent(`Error: ${message.message}`);
  }
}

function formatObsStatus(obs) {
  if (!obs) {
    return "Unknown";
  }
  if (obs.last_error) {
    return `OBS unavailable: ${obs.last_error}`;
  }
  if (obs.streaming === true) {
    return "Streaming";
  }
  if (obs.streaming === false) {
    return "Stopped";
  }
  return `${obs.host}:${obs.port}`;
}

function updateVideo(state) {
  videoStatus.textContent = state.status_text;

  if (!state.can_control) {
    videoFrameHost.replaceChildren();
    videoFrameHost.dataset.src = "";
    videoFrameHost.classList.add("empty");
    const placeholder = document.createElement("div");
    placeholder.className = "video-placeholder";
    placeholder.textContent = state.status_text;
    videoFrameHost.appendChild(placeholder);
    return;
  }

  videoFrameHost.classList.remove("empty");
  if (videoFrameHost.dataset.src === state.stream_url && videoFrameHost.querySelector("iframe")) {
    return;
  }

  const iframe = document.createElement("iframe");
  iframe.src = state.stream_url;
  iframe.title = "Pachislot WebRTC video";
  iframe.allow = "autoplay; fullscreen; picture-in-picture";
  iframe.referrerPolicy = "no-referrer";
  videoFrameHost.replaceChildren(iframe);
  videoFrameHost.dataset.src = state.stream_url;
}

function updateControls() {
  takeControlButton.disabled = canControl;
  quitButton.disabled = !canControl;
  controlButtons.forEach((button) => {
    button.disabled = !canControl;
  });
}

function sendCommand(command) {
  if (!canControl || !(command in COMMAND_LABELS)) {
    return;
  }
  send({ type: "command", command });
}

function pulseButton(command) {
  const button = document.querySelector(`[data-command="${command}"]`);
  if (!button) {
    return;
  }
  button.classList.add("pressed");
  setTimeout(() => button.classList.remove("pressed"), 140);
}

function addEvent(text) {
  const item = document.createElement("li");
  item.textContent = `${new Date().toLocaleTimeString()} - ${text}`;
  events.prepend(item);
  while (events.children.length > 30) {
    events.lastElementChild.remove();
  }
}

takeControlButton.addEventListener("click", () => {
  send({ type: "take_control" });
});

quitButton.addEventListener("click", () => {
  send({ type: "release_control" });
});

controlButtons.forEach((button) => {
  button.addEventListener("click", () => {
    sendCommand(button.dataset.command);
  });
});

window.addEventListener("keydown", (event) => {
  const key = event.key.toLowerCase();
  if (event.repeat || event.ctrlKey || event.altKey || event.metaKey) {
    return;
  }
  if (key in COMMAND_LABELS) {
    event.preventDefault();
    sendCommand(key);
  }
});

setInterval(() => {
  send({ type: "heartbeat" });
}, 12000);

connect();
