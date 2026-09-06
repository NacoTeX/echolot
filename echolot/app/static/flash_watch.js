// Shows which step the USB flash is actually on.
//
// ESP Web Tools' dialog renders both `initializing` (the serial handshake
// with the chip) and `preparing` (downloading the image) as the same
// sentence, "Preparing installation", and puts no timeout on either. A
// flash that sits there for ten minutes therefore gives no clue whether
// the chip or the network is the problem — and the two need opposite
// responses.
//
// The dialog keeps its progress in `_installState` and dispatches no event
// we can subscribe to (its `state-changed` belongs to the Improv serial
// client, not to flashing). So we read that property. It is a private
// field of a vendored element: every access is guarded, and when it is
// missing this file renders nothing rather than breaking the page.

const FLASH_STEPS = {
  initializing: {
    title: "Verbindung zum Chip wird aufgebaut",
    stallAfter: 20,
    hint:
      "Der Chip antwortet nicht. Meist ist er nicht im Download-Modus: " +
      "BOOT gedrückt halten, RESET (EN) kurz drücken, BOOT loslassen. " +
      "Danach den Port neu auswählen — im Download-Modus meldet sich der " +
      "Chip als anderes USB-Gerät an. BOOT ist GPIO0 bei ESP32, S2 und S3, " +
      "GPIO9 bei C3, C5 und C6. Eine bereits installierte Firmware ist " +
      "nie die Ursache: geschrieben wird über den ROM-Bootloader, den " +
      "keine Firmware überschreiben kann.",
  },
  preparing: {
    title: "Firmware wird geladen",
    stallAfter: 20,
    hint:
      "Hier hängt der Download der Firmware, nicht der Chip. " +
      "„Firmware herunterladen“ auf dieser Karte holt dieselbe Datei — " +
      "schreiben lässt sie sich mit esptool auf Offset 0 oder über " +
      "web.esphome.io.",
  },
  erasing: {
    title: "Flash wird gelöscht",
    // Erasing a large flash genuinely takes this long, so the hint has to
    // wait much longer than the two steps above before it says anything.
    stallAfter: 120,
    hint:
      "Ein vollständiges Löschen dauert bei großem Flash bis zu zwei " +
      "Minuten. Erst danach lohnt es sich abzubrechen.",
  },
  writing: {
    title: "Firmware wird geschrieben",
    stallAfter: 90,
    hint:
      "Seit anderthalb Minuten kein Fortschritt. Kabel und Stromversorgung " +
      "prüfen — ein reines Ladekabel ohne Datenadern bricht genau hier ab.",
  },
  finished: { title: "Fertig geflasht", stallAfter: null, hint: "" },
};

// A step counts as stalled only while nothing about it changes, so the
// signature includes the write percentage: a slow write keeps resetting
// the clock, a frozen one does not.
function flashSignature(installState) {
  if (!installState || typeof installState.state !== "string") return "";
  const details = installState.details || {};
  const pct = typeof details.percentage === "number" ? details.percentage : "";
  return `${installState.state}:${pct}`;
}

function describeFlashState(installState, secondsInStep) {
  if (!installState || typeof installState.state !== "string") return null;
  const state = installState.state;

  if (state === "error") {
    return {
      tone: "err",
      title: "Flashen fehlgeschlagen",
      detail: typeof installState.message === "string" ? installState.message : "",
      hint: "",
    };
  }

  const step = FLASH_STEPS[state];
  if (!step) return null;

  const details = installState.details || {};
  let title = step.title;
  if (state === "writing" && typeof details.percentage === "number") {
    title = `${step.title} — ${details.percentage}\u00a0%`;
  }

  const stalled =
    step.stallAfter !== null && secondsInStep >= step.stallAfter;

  return {
    tone: state === "finished" ? "ok" : "pending",
    title,
    detail: stalled ? `seit ${Math.round(secondsInStep)}\u00a0s ohne Fortschritt` : "",
    hint: stalled ? step.hint : "",
  };
}

// ---------------------------------------------------------------------------
// Browser wiring. Everything above is pure so it can be tested in Node;
// this half only runs where there is a document.

if (typeof document !== "undefined") {
  const POLL_MS = 500;
  // The dialog is created by the install button's own click handler and
  // appended to <body>, with no reference back to the card it came from.
  // Remembering the card on the way down through the capture phase is the
  // only link between the two.
  let pendingCard = null;

  document.addEventListener(
    "click",
    (event) => {
      const button = event.target.closest
        ? event.target.closest("esp-web-install-button")
        : null;
      if (button) pendingCard = button.closest(".device-card");
    },
    true,
  );

  function renderFlashProgress(card, view) {
    const box = card && card.querySelector(".flash-progress");
    if (!box) return;
    if (!view) {
      box.hidden = true;
      box.textContent = "";
      return;
    }
    box.hidden = false;
    box.className = `flash-progress status status-${view.tone}`;
    box.textContent = "";

    const title = document.createElement("strong");
    title.textContent = view.detail ? `${view.title} (${view.detail})` : view.title;
    box.appendChild(title);

    if (view.hint) {
      const hint = document.createElement("span");
      hint.className = "flash-hint";
      hint.textContent = view.hint;
      box.appendChild(hint);
    }
  }

  function watchDialog(dialog, card) {
    let signature = null;
    let since = Date.now();

    const timer = setInterval(() => {
      if (!dialog.isConnected) {
        clearInterval(timer);
        renderFlashProgress(card, null);
        return;
      }
      // Private field of a vendored element: absent on any version that
      // renames it, in which case this simply shows nothing.
      const installState = dialog._installState;
      const current = flashSignature(installState);
      if (current !== signature) {
        signature = current;
        since = Date.now();
      }
      renderFlashProgress(
        card,
        describeFlashState(installState, (Date.now() - since) / 1000),
      );
    }, POLL_MS);
  }

  new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeName !== "EWT-INSTALL-DIALOG") continue;
        const card = pendingCard;
        pendingCard = null;
        if (card) watchDialog(node, card);
      }
    }
  }).observe(document.body, { childList: true });
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { FLASH_STEPS, flashSignature, describeFlashState };
}
