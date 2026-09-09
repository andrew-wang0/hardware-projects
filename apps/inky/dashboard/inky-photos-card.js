class InkyPhotosCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {
      library: "sensor.inky_photo_library",
      busy: "binary_sensor.inky_photo_busy",
      command_topic: "inky/photo/select",
    };
  }

  setConfig(config) {
    this._config = {
      library: config.library || "sensor.inky_photo_library",
      busy: config.busy || "binary_sensor.inky_photo_busy",
      command_topic: config.command_topic || "inky/photo/select",
      title: config.title,
    };
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    const photos = this._photos();
    return Math.max(2, Math.ceil(photos.length / 3) + 1);
  }

  _photos() {
    const state = this._hass?.states?.[this._config.library];
    const photos = state?.attributes?.photos;
    return Array.isArray(photos) ? photos : [];
  }

  _busy() {
    const sensor = this._hass?.states?.[this._config.busy];
    if (sensor?.state === "on") {
      return true;
    }
    const library = this._hass?.states?.[this._config.library];
    return library?.attributes?.busy === true;
  }

  _picture(entityId) {
    const entity = this._hass?.states?.[entityId];
    return entity?.attributes?.entity_picture || "";
  }

  _select(filename) {
    if (!this._hass || this._busy()) {
      return;
    }
    this._hass.callService("mqtt", "publish", {
      topic: this._config.command_topic,
      payload: filename,
      qos: 1,
    });
  }

  _render() {
    if (this.shadowRoot === null) {
      return;
    }
    const photos = this._photos();
    const busy = this._busy();
    const title = this._config.title
      ? `<div class="title">${escapeHtml(this._config.title)}</div>`
      : "";
    const status = busy
      ? `<div class="status">Inky is updating the display</div>`
      : "";
    const body =
      photos.length === 0
        ? `<div class="empty">No stored photos yet</div>`
        : `<div class="grid">${photos
            .map((photo) => this._photoButton(photo, busy))
            .join("")}</div>`;

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card {
          padding: 12px;
        }
        .title {
          font-size: 18px;
          font-weight: 500;
          margin: 0 4px 12px;
        }
        .status, .empty {
          margin: 0 4px 12px;
          color: var(--secondary-text-color);
          font-size: 14px;
        }
        .grid {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(148px, 1fr));
          gap: 10px;
        }
        button.photo {
          display: flex;
          flex-direction: column;
          padding: 0;
          border: 2px solid transparent;
          border-radius: 12px;
          overflow: hidden;
          background: var(--secondary-background-color, var(--card-background-color));
          color: var(--primary-text-color);
          cursor: pointer;
        }
        button.photo.current {
          border-color: var(--primary-color);
        }
        button.photo:disabled {
          cursor: not-allowed;
          opacity: 0.6;
        }
        button.photo img,
        button.photo .placeholder {
          display: block;
          width: 100%;
          aspect-ratio: 5 / 3;
          object-fit: cover;
          background: var(--divider-color);
        }
        button.photo .placeholder {
          display: flex;
          align-items: center;
          justify-content: center;
          color: var(--secondary-text-color);
          font-size: 12px;
        }
        button.photo .label {
          padding: 8px;
          font-size: 12px;
          line-height: 1.3;
          text-align: center;
        }
        button.photo .on-panel {
          display: block;
          margin-top: 2px;
          color: var(--primary-color);
          font-size: 11px;
        }
      </style>
      <ha-card>
        ${title}
        ${status}
        ${body}
      </ha-card>
    `;

    this.shadowRoot.querySelectorAll("button.photo[data-filename]").forEach((button) => {
      button.addEventListener("click", () => {
        this._select(button.dataset.filename);
      });
    });
  }

  _photoButton(photo, busy) {
    const filename = String(photo.filename || "");
    const label = String(photo.label || filename);
    const entityId = String(photo.entity_id || "");
    const current = photo.current === true;
    const picture = this._picture(entityId);
    const image = picture
      ? `<img src="${escapeHtml(picture)}" alt="${escapeHtml(label)}">`
      : `<div class="placeholder">No image</div>`;
    const badge = current ? `<span class="on-panel">On panel</span>` : "";
    const disabled = busy || current ? "disabled" : "";
    const currentClass = current ? " current" : "";
    return `
      <button class="photo${currentClass}" type="button" data-filename="${escapeHtml(filename)}" ${disabled}>
        ${image}
        <div class="label">${escapeHtml(label)}${badge}</div>
      </button>
    `;
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

customElements.define("inky-photos-card", InkyPhotosCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "inky-photos-card",
  name: "Inky Photos",
  description: "Choose a stored Inky photo by tapping its picture",
});
