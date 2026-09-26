/* @ds-bundle: {"format":4,"namespace":"UEPImaginarySpaceDesignSystem_6b2a32","components":[{"name":"ArchCard","sourcePath":"components/content/ArchCard.jsx"},{"name":"ChapterTimeline","sourcePath":"components/content/ChapterTimeline.jsx"},{"name":"NoteSheet","sourcePath":"components/content/NoteSheet.jsx"},{"name":"Prose","sourcePath":"components/content/Prose.jsx"},{"name":"BrandMark","sourcePath":"components/core/BrandMark.jsx"},{"name":"Button","sourcePath":"components/core/Button.jsx"},{"name":"Hairline","sourcePath":"components/core/Hairline.jsx"},{"name":"MonoLabel","sourcePath":"components/core/MonoLabel.jsx"},{"name":"UepDialogue","sourcePath":"components/core/UepDialogue.jsx"},{"name":"UepVoice","sourcePath":"components/core/UepVoice.jsx"},{"name":"EchoAppreciation","sourcePath":"components/echoes/EchoAppreciation.jsx"},{"name":"EchoOrbCard","sourcePath":"components/echoes/EchoOrbCard.jsx"},{"name":"EchoPlayer","sourcePath":"components/echoes/EchoPlayer.jsx"},{"name":"EchoPlaylist","sourcePath":"components/echoes/EchoPlaylist.jsx"},{"name":"EchoSubcatRow","sourcePath":"components/echoes/EchoSubcatRow.jsx"},{"name":"EchoVinyl","sourcePath":"components/echoes/EchoVinyl.jsx"},{"name":"Dialog","sourcePath":"components/feedback/Dialog.jsx"},{"name":"RitualPanel","sourcePath":"components/feedback/RitualPanel.jsx"},{"name":"Toast","sourcePath":"components/feedback/Toast.jsx"},{"name":"ToastStack","sourcePath":"components/feedback/Toast.jsx"},{"name":"ZoneState","sourcePath":"components/feedback/ZoneState.jsx"},{"name":"Breadcrumb","sourcePath":"components/navigation/Breadcrumb.jsx"},{"name":"NavTree","sourcePath":"components/navigation/NavTree.jsx"},{"name":"PrevNext","sourcePath":"components/navigation/PrevNext.jsx"},{"name":"TopBar","sourcePath":"components/navigation/TopBar.jsx"},{"name":"VisCrossroad","sourcePath":"components/visuals/VisCrossroad.jsx"},{"name":"VisGallery","sourcePath":"components/visuals/VisGallery.jsx"},{"name":"VisGalleryCard","sourcePath":"components/visuals/VisGalleryCard.jsx"},{"name":"VisLightbox","sourcePath":"components/visuals/VisLightbox.jsx"},{"name":"VisSpriteViewer","sourcePath":"components/visuals/VisSpriteViewer.jsx"},{"name":"VisSubcatBoard","sourcePath":"components/visuals/VisSubcatBoard.jsx"}],"sourceHashes":{"components/content/ArchCard.jsx":"adad71704157","components/content/ChapterTimeline.jsx":"e8738f5a19e6","components/content/NoteSheet.jsx":"8e856a109bc1","components/content/Prose.jsx":"5e9154088335","components/core/BrandMark.jsx":"19dd5cc051c6","components/core/Button.jsx":"e5bdf4d75c0d","components/core/Hairline.jsx":"8e272dafb13a","components/core/MonoLabel.jsx":"4f39496b4c93","components/core/UepDialogue.jsx":"6295c5eb73ec","components/core/UepVoice.jsx":"06181de4c44d","components/echoes/EchoAppreciation.jsx":"a87fd53d3906","components/echoes/EchoOrbCard.jsx":"5d6c51d32c4f","components/echoes/EchoPlayer.jsx":"24f59e66a461","components/echoes/EchoPlaylist.jsx":"da4e24db4c28","components/echoes/EchoSubcatRow.jsx":"4708b3ed1ba9","components/echoes/EchoVinyl.jsx":"867c4ce79c8b","components/feedback/Dialog.jsx":"0ea1d3432e59","components/feedback/RitualPanel.jsx":"7999d565f067","components/feedback/Toast.jsx":"7c4b86d6e928","components/feedback/ZoneState.jsx":"896a6bc351dd","components/navigation/Breadcrumb.jsx":"acef44544f12","components/navigation/NavTree.jsx":"a5ae417c5e10","components/navigation/PrevNext.jsx":"62696bb856b4","components/navigation/TopBar.jsx":"bd25a8883e2e","components/visuals/VisCrossroad.jsx":"47aad2bdcb31","components/visuals/VisGallery.jsx":"8c940435f065","components/visuals/VisGalleryCard.jsx":"8fab47b82635","components/visuals/VisLightbox.jsx":"570908ff1866","components/visuals/VisSpriteViewer.jsx":"b67e8bf5afd7","components/visuals/VisSubcatBoard.jsx":"bb317b944bd3","ui_kits/uep-docs/HomeScreen.jsx":"7570a55a454c","ui_kits/uep-docs/ReaderScreen.jsx":"82ea44d9c173","ui_kits/uep-docs/ZoneEntryScreen.jsx":"7aa3653c1542","ui_kits/uep-docs/data.js":"a2e239026ec2"},"inlinedExternals":[],"unexposedExports":[]} */

(() => {

const __ds_ns = (window.UEPImaginarySpaceDesignSystem_6b2a32 = window.UEPImaginarySpaceDesignSystem_6b2a32 || {});

const __ds_scope = {};

(__ds_ns.__errors = __ds_ns.__errors || []);

// components/content/ArchCard.jsx
try { (() => {
function ArchCard({
  index,
  title,
  meta,
  locked = false,
  onClick
}) {
  return /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: 'uep-arch-card' + (locked ? ' is-locked' : ''),
    onClick: locked ? undefined : onClick
  }, index != null ? /*#__PURE__*/React.createElement("div", {
    className: "uep-arch-index"
  }, index) : null, /*#__PURE__*/React.createElement("div", {
    className: "uep-arch-title"
  }, locked ? '？？？' : title), meta ? /*#__PURE__*/React.createElement("div", {
    className: "uep-arch-meta"
  }, meta) : null);
}
Object.assign(__ds_scope, { ArchCard });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/content/ArchCard.jsx", error: String((e && e.message) || e) }); }

// components/content/ChapterTimeline.jsx
try { (() => {
const DOT = {
  completed: '',
  available: '',
  progression: '',
  flag: '❖',
  static: '🔒'
};
function ChapterTimeline({
  items = [],
  currentId,
  onSelect
}) {
  return /*#__PURE__*/React.createElement("ol", {
    className: "uep-timeline"
  }, items.map(item => {
    const state = item.state || 'available';
    const isCurrent = item.id === currentId;
    const locked = state === 'progression' || state === 'flag' || state === 'static';
    return /*#__PURE__*/React.createElement("li", {
      className: `uep-timeline-item is-${state}`,
      key: item.id
    }, /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "uep-timeline-button",
      disabled: locked,
      onClick: locked || !onSelect ? undefined : () => onSelect(item)
    }, /*#__PURE__*/React.createElement("span", {
      className: "uep-timeline-marker"
    }, /*#__PURE__*/React.createElement("span", {
      className: `uep-timeline-dot is-${state}${isCurrent ? ' is-current' : ''}`
    }, DOT[state])), /*#__PURE__*/React.createElement("span", {
      className: "uep-timeline-body"
    }, /*#__PURE__*/React.createElement("span", {
      className: "uep-timeline-title"
    }, state === 'flag' ? '？？？' : item.title), item.desc ? /*#__PURE__*/React.createElement("span", {
      className: "uep-timeline-desc"
    }, item.desc) : null)));
  }));
}
Object.assign(__ds_scope, { ChapterTimeline });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/content/ChapterTimeline.jsx", error: String((e && e.message) || e) }); }

// components/content/NoteSheet.jsx
try { (() => {
function NoteSheet({
  kicker,
  title,
  children
}) {
  return /*#__PURE__*/React.createElement("div", {
    className: "uep-note"
  }, kicker ? /*#__PURE__*/React.createElement("div", {
    className: "uep-label"
  }, kicker) : null, title ? /*#__PURE__*/React.createElement("h3", null, title) : null, /*#__PURE__*/React.createElement("div", {
    className: "uep-note__body"
  }, children));
}
Object.assign(__ds_scope, { NoteSheet });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/content/NoteSheet.jsx", error: String((e && e.message) || e) }); }

// components/content/Prose.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function Prose({
  unwritten = false,
  html,
  children,
  ...rest
}) {
  const cls = 'uep-prose' + (unwritten ? ' uep-prose--unwritten' : '');
  if (html != null) {
    return /*#__PURE__*/React.createElement("div", _extends({
      className: cls,
      dangerouslySetInnerHTML: {
        __html: html
      }
    }, rest));
  }
  return /*#__PURE__*/React.createElement("div", _extends({
    className: cls
  }, rest), children);
}
Object.assign(__ds_scope, { Prose });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/content/Prose.jsx", error: String((e && e.message) || e) }); }

// components/core/BrandMark.jsx
try { (() => {
function BrandMark({
  size = 28,
  glyph = 'U',
  title,
  subtitle,
  href = '#'
}) {
  const mark = /*#__PURE__*/React.createElement("div", {
    className: "uep-brand-mark",
    style: {
      width: size,
      height: size,
      fontSize: Math.round(size * 0.5)
    }
  }, glyph);
  if (!title) return mark;
  return /*#__PURE__*/React.createElement("a", {
    className: "uep-topbar__brand",
    href: href
  }, mark, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
    className: "uep-brand-title"
  }, title), subtitle ? /*#__PURE__*/React.createElement("div", {
    className: "uep-brand-subtitle"
  }, subtitle) : null));
}
Object.assign(__ds_scope, { BrandMark });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/BrandMark.jsx", error: String((e && e.message) || e) }); }

// components/core/Button.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function Button({
  variant = 'outline',
  size = 'md',
  children,
  ...rest
}) {
  const cls = variant === 'zone' ? 'btn-zone' : variant === 'terminal' ? 'btn-terminal' : variant === 'gold' ? 'btn-outline btn-outline--gold' : 'btn-outline';
  const sizeCls = variant === 'outline' || variant === 'gold' ? size === 'sm' ? ' btn-outline--sm' : '' : '';
  return /*#__PURE__*/React.createElement("button", _extends({
    type: "button",
    className: cls + sizeCls
  }, rest), children);
}
Object.assign(__ds_scope, { Button });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Button.jsx", error: String((e && e.message) || e) }); }

// components/core/Hairline.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function Hairline({
  variant = 'plain',
  width,
  style,
  ...rest
}) {
  if (variant === 'gold') {
    return /*#__PURE__*/React.createElement("hr", _extends({
      style: {
        border: 0,
        height: 1,
        margin: '38px auto',
        maxWidth: width || 280,
        background: 'linear-gradient(90deg,transparent,var(--uep-gold),transparent)',
        opacity: 0.55,
        ...style
      }
    }, rest));
  }
  if (variant === 'zone') {
    return /*#__PURE__*/React.createElement("hr", _extends({
      style: {
        border: 0,
        height: 1,
        margin: '38px 0',
        width: width || '100%',
        background: 'linear-gradient(90deg,transparent,var(--zone-main,var(--uep-gold)),transparent)',
        opacity: 0.55,
        ...style
      }
    }, rest));
  }
  return /*#__PURE__*/React.createElement("hr", _extends({
    className: variant === 'strong' ? 'hairline-thick' : 'hairline',
    style: style
  }, rest));
}
Object.assign(__ds_scope, { Hairline });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Hairline.jsx", error: String((e && e.message) || e) }); }

// components/core/MonoLabel.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function MonoLabel({
  tone = 'mute',
  track = 'wide',
  as: As = 'div',
  children,
  ...rest
}) {
  const cls = ['uep-label', tone === 'zone' ? 'uep-label--zone' : tone === 'gold' ? 'uep-label--gold' : '', track === 'ritual' ? 'uep-label--ritual' : track === 'crumb' ? 'uep-label--crumb' : ''].filter(Boolean).join(' ');
  return /*#__PURE__*/React.createElement(As, _extends({
    className: cls
  }, rest), children);
}
Object.assign(__ds_scope, { MonoLabel });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/MonoLabel.jsx", error: String((e && e.message) || e) }); }

// components/core/UepDialogue.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function UepDialogue({
  side = 'left',
  children,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    "data-role": "uep",
    "data-side": side === 'right' ? 'right' : undefined
  }, rest), children);
}
Object.assign(__ds_scope, { UepDialogue });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/UepDialogue.jsx", error: String((e && e.message) || e) }); }

// components/core/UepVoice.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function UepVoice({
  effect = 'plain',
  as: As = 'span',
  children,
  ...rest
}) {
  const cls = 'uep-voice' + (effect !== 'plain' ? ` uep-voice--${effect}` : '');
  return /*#__PURE__*/React.createElement(As, _extends({
    className: cls
  }, rest), children);
}
Object.assign(__ds_scope, { UepVoice });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/UepVoice.jsx", error: String((e && e.message) || e) }); }

// components/echoes/EchoAppreciation.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function EchoAppreciation({
  label = 'APPRECIATION',
  children,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    className: "echoes-appreciation"
  }, rest), /*#__PURE__*/React.createElement("div", {
    className: "echoes-appreciation-label"
  }, label), /*#__PURE__*/React.createElement("div", {
    className: "echoes-appreciation-body"
  }, children));
}
Object.assign(__ds_scope, { EchoAppreciation });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/echoes/EchoAppreciation.jsx", error: String((e && e.message) || e) }); }

// components/echoes/EchoOrbCard.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function EchoOrbCard({
  name,
  desc,
  meta,
  color = 'var(--echoes-main)',
  particles = 8,
  ...rest
}) {
  const dots = Array.from({
    length: particles
  }, (_, i) => {
    const angle = i / particles * Math.PI * 2 - Math.PI / 2;
    const r = 44;
    return {
      left: 55 + Math.cos(angle) * r - 5,
      top: 55 + Math.sin(angle) * r - 5,
      delay: i / particles * 3,
      size: i % 3 === 0 ? 12 : 9
    };
  });
  return /*#__PURE__*/React.createElement("button", _extends({
    type: "button",
    className: "echoes-cluster-card",
    style: {
      '--cluster-color': color
    }
  }, rest), /*#__PURE__*/React.createElement("div", {
    className: "echoes-orb-field",
    "aria-hidden": "true"
  }, dots.map((d, i) => /*#__PURE__*/React.createElement("span", {
    key: i,
    className: "echoes-orb-particle",
    style: {
      left: d.left,
      top: d.top,
      width: d.size,
      height: d.size,
      background: color,
      animationDelay: d.delay + 's'
    }
  })), /*#__PURE__*/React.createElement("span", {
    className: "echoes-orb-center",
    style: {
      background: color,
      boxShadow: '0 0 18px 4px ' + color
    }
  })), /*#__PURE__*/React.createElement("div", {
    className: "echoes-cluster-text"
  }, /*#__PURE__*/React.createElement("div", {
    className: "echoes-cluster-name"
  }, name), desc ? /*#__PURE__*/React.createElement("div", {
    className: "echoes-cluster-desc"
  }, desc) : null), meta ? /*#__PURE__*/React.createElement("div", {
    className: "echoes-cluster-meta"
  }, meta) : null);
}
Object.assign(__ds_scope, { EchoOrbCard });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/echoes/EchoOrbCard.jsx", error: String((e && e.message) || e) }); }

// components/echoes/EchoPlayer.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function EchoPlayer({
  playing = false,
  progress = 0.34,
  current = '1:24',
  total = '4:12',
  status = 'STREAMING \u00B7 LOCAL CACHE',
  queueButton = true,
  ...rest
}) {
  const pct = Math.max(0, Math.min(1, progress)) * 100;
  return /*#__PURE__*/React.createElement("div", _extends({
    className: "echoes-player"
  }, rest), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "echoes-player-btn",
    "aria-label": playing ? 'Pause' : 'Play'
  }, /*#__PURE__*/React.createElement("span", {
    className: 'echoes-player-icon ' + (playing ? 'echoes-player-icon-pause' : 'echoes-player-icon-play')
  })), /*#__PURE__*/React.createElement("div", {
    className: "echoes-player-bar"
  }, /*#__PURE__*/React.createElement("div", {
    className: "echoes-player-track"
  }, /*#__PURE__*/React.createElement("div", {
    className: "echoes-player-fill",
    style: {
      width: pct + '%'
    }
  }), /*#__PURE__*/React.createElement("div", {
    className: "echoes-player-thumb",
    style: {
      left: pct + '%'
    }
  })), /*#__PURE__*/React.createElement("div", {
    className: "echoes-player-times"
  }, /*#__PURE__*/React.createElement("span", null, current), /*#__PURE__*/React.createElement("span", null, total))), /*#__PURE__*/React.createElement("div", {
    className: "echoes-player-actions"
  }, queueButton ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "echoes-player-queue-btn",
    "aria-label": "Add to queue"
  }, /*#__PURE__*/React.createElement("svg", {
    viewBox: "0 0 24 24"
  }, /*#__PURE__*/React.createElement("path", {
    d: "M12 5v14M5 12h14"
  }))) : null, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "echoes-player-vol-btn",
    "aria-label": "Volume"
  }, /*#__PURE__*/React.createElement("svg", {
    className: "echoes-player-volume-icon",
    viewBox: "0 0 16 16"
  }, /*#__PURE__*/React.createElement("path", {
    className: "echoes-player-volume-body",
    d: "M3 6h2.2L8.5 3.2v9.6L5.2 10H3z"
  }), /*#__PURE__*/React.createElement("path", {
    className: "echoes-player-volume-wave",
    d: "M10.8 5.6a3.4 3.4 0 0 1 0 4.8M12.7 3.7a6 6 0 0 1 0 8.6"
  })))), /*#__PURE__*/React.createElement("div", {
    className: "echoes-player-status"
  }, status));
}
Object.assign(__ds_scope, { EchoPlayer });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/echoes/EchoPlayer.jsx", error: String((e && e.message) || e) }); }

// components/echoes/EchoPlaylist.jsx
try { (() => {
function EchoPlaylist({
  label = 'TRACKS',
  count,
  items = [],
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", rest, /*#__PURE__*/React.createElement("div", {
    className: "echoes-playlist-header"
  }, /*#__PURE__*/React.createElement("p", {
    className: "echoes-kicker",
    style: {
      marginBottom: 0
    }
  }, label, count != null ? ' \u00B7 ' + count : '')), items.map((it, i) => /*#__PURE__*/React.createElement("button", {
    key: it.id || i,
    type: "button",
    className: 'echoes-playlist-item' + (it.locked ? ' is-locked' : ''),
    disabled: it.locked
  }, /*#__PURE__*/React.createElement("span", {
    className: "echoes-playlist-num"
  }, String(i + 1).padStart(2, '0')), /*#__PURE__*/React.createElement("span", {
    className: "echoes-playlist-info"
  }, /*#__PURE__*/React.createElement("span", {
    className: "echoes-playlist-title"
  }, it.title), it.sub ? /*#__PURE__*/React.createElement("span", {
    className: "echoes-playlist-sub"
  }, it.sub) : null, it.story ? /*#__PURE__*/React.createElement("span", {
    className: "echoes-playlist-story"
  }, '\u25C8 ' + it.story) : null), /*#__PURE__*/React.createElement("span", {
    className: "echoes-playlist-dur"
  }, it.duration), /*#__PURE__*/React.createElement("span", {
    className: "echoes-playlist-arrow"
  }, it.locked ? '\u25A1' : '\u203A'))));
}
Object.assign(__ds_scope, { EchoPlaylist });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/echoes/EchoPlaylist.jsx", error: String((e && e.message) || e) }); }

// components/echoes/EchoSubcatRow.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function EchoSubcatRow({
  num,
  name,
  hint,
  count,
  color,
  arrow = '\u2192',
  ...rest
}) {
  return /*#__PURE__*/React.createElement("button", _extends({
    type: "button",
    className: "echoes-subcat-card",
    style: color ? {
      '--row-color': color
    } : undefined
  }, rest), /*#__PURE__*/React.createElement("span", {
    className: "echoes-subcat-num"
  }, num), /*#__PURE__*/React.createElement("span", null, /*#__PURE__*/React.createElement("span", {
    className: "echoes-subcat-name"
  }, name), hint ? /*#__PURE__*/React.createElement("span", {
    className: "echoes-subcat-hint",
    style: {
      display: 'block'
    }
  }, hint) : null), /*#__PURE__*/React.createElement("span", {
    className: "echoes-subcat-count"
  }, count), /*#__PURE__*/React.createElement("span", {
    className: "echoes-subcat-arrow"
  }, arrow));
}
Object.assign(__ds_scope, { EchoSubcatRow });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/echoes/EchoSubcatRow.jsx", error: String((e && e.message) || e) }); }

// components/echoes/EchoVinyl.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function EchoVinyl({
  cover,
  spinning = false,
  locked = false,
  lockLabel = 'LOCKED',
  size = 240,
  rings = 4,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    className: "echoes-vinyl-wrap",
    style: {
      width: size,
      height: size
    }
  }, rest), /*#__PURE__*/React.createElement("div", {
    className: 'echoes-vinyl' + (spinning ? ' is-spinning' : ''),
    style: cover ? {
      backgroundImage: 'url(' + cover + ')'
    } : undefined
  }, Array.from({
    length: rings
  }, (_, i) => /*#__PURE__*/React.createElement("span", {
    key: i,
    className: "echoes-vinyl-ring",
    style: {
      inset: 8 + i * 11 + '%'
    }
  }))), /*#__PURE__*/React.createElement("div", {
    className: "echoes-vinyl-center"
  }, /*#__PURE__*/React.createElement("span", {
    className: "echoes-vinyl-hole"
  })), locked ? /*#__PURE__*/React.createElement("div", {
    className: "echoes-vinyl-lock"
  }, /*#__PURE__*/React.createElement("span", null, lockLabel)) : null);
}
Object.assign(__ds_scope, { EchoVinyl });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/echoes/EchoVinyl.jsx", error: String((e && e.message) || e) }); }

// components/feedback/Dialog.jsx
try { (() => {
function Dialog({
  variant = 'default',
  title,
  message,
  input,
  placeholder,
  confirmLabel = '確認',
  cancelLabel = '取消',
  onConfirm,
  onCancel
}) {
  const term = variant === 'terminal';
  return /*#__PURE__*/React.createElement("div", {
    className: 'uep-dialog-overlay' + (term ? ' uep-dialog-overlay--terminal' : '')
  }, /*#__PURE__*/React.createElement("div", {
    className: 'uep-dialog' + (term ? ' uep-dialog--terminal' : '')
  }, /*#__PURE__*/React.createElement("span", {
    className: "uep-dialog__accent"
  }), title ? /*#__PURE__*/React.createElement("h2", {
    className: "uep-dialog__title"
  }, title) : null, message ? /*#__PURE__*/React.createElement("p", {
    className: "uep-dialog__message"
  }, message) : null, input ? /*#__PURE__*/React.createElement("input", {
    className: "uep-dialog__input",
    placeholder: placeholder
  }) : null, /*#__PURE__*/React.createElement("div", {
    className: "uep-dialog__actions"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "uep-dialog__btn uep-dialog__btn--cancel",
    onClick: onCancel
  }, cancelLabel), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "uep-dialog__btn uep-dialog__btn--confirm",
    onClick: onConfirm
  }, confirmLabel))));
}
Object.assign(__ds_scope, { Dialog });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/Dialog.jsx", error: String((e && e.message) || e) }); }

// components/feedback/RitualPanel.jsx
try { (() => {
function RitualPanel({
  aperture = '◎',
  kicker = 'OBSERVER PROTOCOL',
  title,
  lines = [],
  stayLabel = '留下',
  becomeLabel = '成為觀測者',
  becomeDisabled = false,
  onStay,
  onBecome
}) {
  return /*#__PURE__*/React.createElement("div", {
    className: "uep-viewgate"
  }, /*#__PURE__*/React.createElement("div", {
    className: "uep-viewgate__veil"
  }), /*#__PURE__*/React.createElement("div", {
    className: "uep-viewgate__panel"
  }, /*#__PURE__*/React.createElement("div", {
    className: "uep-viewgate__aperture"
  }, aperture), /*#__PURE__*/React.createElement("div", {
    className: "uep-viewgate__kicker"
  }, kicker), /*#__PURE__*/React.createElement("h2", {
    className: "uep-viewgate__title"
  }, title), /*#__PURE__*/React.createElement("div", {
    className: "uep-viewgate__lines"
  }, lines.map((line, i) => /*#__PURE__*/React.createElement("p", {
    className: "uep-viewgate__line",
    key: i
  }, line))), /*#__PURE__*/React.createElement("div", {
    className: "uep-viewgate__actions"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "uep-viewgate__btn uep-viewgate__btn--stay",
    onClick: onStay
  }, stayLabel), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "uep-viewgate__btn uep-viewgate__btn--become",
    disabled: becomeDisabled,
    onClick: onBecome
  }, becomeLabel))));
}
Object.assign(__ds_scope, { RitualPanel });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/RitualPanel.jsx", error: String((e && e.message) || e) }); }

// components/feedback/Toast.jsx
try { (() => {
const ICONS = {
  success: '✓',
  error: '✕',
  warning: '❖',
  info: 'i'
};
function Toast({
  type = 'info',
  message,
  onClose
}) {
  return /*#__PURE__*/React.createElement("div", {
    className: `uep-toast uep-toast--${type}`
  }, /*#__PURE__*/React.createElement("span", {
    className: "uep-toast__icon"
  }, ICONS[type]), /*#__PURE__*/React.createElement("p", {
    className: "uep-toast__msg"
  }, message), onClose ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "uep-toast__close",
    onClick: onClose,
    "aria-label": "\u95DC\u9589"
  }, "\u2715") : null);
}
function ToastStack({
  toasts = [],
  onClose
}) {
  return /*#__PURE__*/React.createElement("div", {
    className: "uep-toast-container"
  }, toasts.map(t => /*#__PURE__*/React.createElement(Toast, {
    key: t.id,
    type: t.type,
    message: t.message,
    onClose: onClose ? () => onClose(t.id) : undefined
  })));
}
Object.assign(__ds_scope, { Toast, ToastStack });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/Toast.jsx", error: String((e && e.message) || e) }); }

// components/feedback/ZoneState.jsx
try { (() => {
function ZoneState({
  state = 'loading',
  message,
  large = false,
  retryLabel = '重試',
  onRetry
}) {
  const text = message || (state === 'loading' ? '載入中…' : state === 'error' ? '內容載入失敗' : '這裡還沒有內容');
  return /*#__PURE__*/React.createElement("div", {
    className: 'zone-state' + (state === 'error' ? ' zone-state--error' : '') + (large ? ' zone-state--large' : '')
  }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("span", {
    className: state === 'empty' ? 'empty-notice' : undefined
  }, text), state === 'error' && onRetry ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: onRetry
  }, retryLabel) : null));
}
Object.assign(__ds_scope, { ZoneState });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/ZoneState.jsx", error: String((e && e.message) || e) }); }

// components/navigation/Breadcrumb.jsx
try { (() => {
function Breadcrumb({
  items = [],
  bordered = false,
  showLine = true,
  onNavigate
}) {
  return /*#__PURE__*/React.createElement("nav", {
    className: 'zone-breadcrumb' + (bordered ? ' zone-breadcrumb--bordered' : '')
  }, showLine ? /*#__PURE__*/React.createElement("span", {
    className: "zone-breadcrumb-line"
  }) : null, items.map((item, i) => /*#__PURE__*/React.createElement(React.Fragment, {
    key: i
  }, i > 0 ? /*#__PURE__*/React.createElement("span", {
    className: "zone-breadcrumb-sep"
  }, "/") : null, i < items.length - 1 ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: onNavigate ? () => onNavigate(item, i) : undefined
  }, item) : /*#__PURE__*/React.createElement("span", null, item))));
}
Object.assign(__ds_scope, { Breadcrumb });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/navigation/Breadcrumb.jsx", error: String((e && e.message) || e) }); }

// components/navigation/NavTree.jsx
try { (() => {
function NavTree({
  items = [],
  currentId,
  expanded = {},
  onToggle,
  onSelect,
  depth = 0
}) {
  return /*#__PURE__*/React.createElement("div", {
    className: depth === 0 ? 'uep-tree' : 'uep-tree-children'
  }, items.map(item => {
    const open = expanded[item.id] !== false;
    const hasKids = item.children && item.children.length > 0;
    const lockCls = item.lock === 'progression' ? ' uep-title--blurred' : item.lock === 'flag' ? ' uep-title--veiled' : '';
    return /*#__PURE__*/React.createElement("div", {
      className: "uep-tree-item",
      key: item.id
    }, /*#__PURE__*/React.createElement("div", {
      className: "uep-tree-row"
    }, hasKids ? /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "uep-tree-chevron",
      onClick: onToggle ? () => onToggle(item.id) : undefined
    }, open ? '−' : '+') : /*#__PURE__*/React.createElement("span", {
      className: "uep-tree-spacer"
    }), /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: 'uep-tree-link' + (item.id === currentId ? ' is-current' : ''),
      onClick: onSelect ? () => onSelect(item) : undefined
    }, /*#__PURE__*/React.createElement("span", {
      className: "uep-tree-kind"
    }, item.kind || ''), /*#__PURE__*/React.createElement("span", {
      className: 'uep-tree-title' + lockCls
    }, item.lock === 'flag' ? '？？？' : item.title))), hasKids && open ? /*#__PURE__*/React.createElement(NavTree, {
      items: item.children,
      currentId: currentId,
      expanded: expanded,
      onToggle: onToggle,
      onSelect: onSelect,
      depth: depth + 1
    }) : null);
  }));
}
Object.assign(__ds_scope, { NavTree });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/navigation/NavTree.jsx", error: String((e && e.message) || e) }); }

// components/navigation/PrevNext.jsx
try { (() => {
function PrevNext({
  prev,
  next,
  prevLabel = '上一篇',
  nextLabel = '下一篇',
  onPrev,
  onNext
}) {
  return /*#__PURE__*/React.createElement("nav", {
    className: "zone-prev-next"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    disabled: !prev,
    onClick: onPrev
  }, /*#__PURE__*/React.createElement("span", null, "\u2190 ", prevLabel), /*#__PURE__*/React.createElement("strong", null, prev || '—')), /*#__PURE__*/React.createElement("button", {
    type: "button",
    disabled: !next,
    onClick: onNext
  }, /*#__PURE__*/React.createElement("span", null, nextLabel, " \u2192"), /*#__PURE__*/React.createElement("strong", null, next || '—')));
}
Object.assign(__ds_scope, { PrevNext });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/navigation/PrevNext.jsx", error: String((e && e.message) || e) }); }

// components/navigation/TopBar.jsx
try { (() => {
function TopBar({
  title = 'Imaginary Space',
  subtitle = '邊際世界 · 觀測誌',
  variant = 'site',
  theme = 'light',
  onToggleTheme,
  onOpenMap,
  actions
}) {
  return /*#__PURE__*/React.createElement("div", {
    className: 'uep-topbar' + (variant === 'reader' ? ' uep-topbar--reader' : '')
  }, /*#__PURE__*/React.createElement(__ds_scope.BrandMark, {
    size: variant === 'reader' ? 29 : 28,
    title: title,
    subtitle: subtitle
  }), /*#__PURE__*/React.createElement("div", {
    className: "uep-topbar__actions"
  }, onOpenMap ? /*#__PURE__*/React.createElement(__ds_scope.Button, {
    variant: "outline",
    size: "sm",
    onClick: onOpenMap
  }, "\u2726 \u5927\u5730\u5716") : null, actions, onToggleTheme ? /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("span", {
    className: "uep-topbar__divider"
  }), /*#__PURE__*/React.createElement(__ds_scope.Button, {
    variant: "outline",
    size: "sm",
    onClick: onToggleTheme
  }, theme === 'dark' ? '☀ 白晝' : '☾ 夜間')) : null));
}
Object.assign(__ds_scope, { TopBar });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/navigation/TopBar.jsx", error: String((e && e.message) || e) }); }

// components/visuals/VisCrossroad.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const VIS_AREAS = [{
  key: 'forward',
  area: 'fwd',
  dir: 'FORWARD'
}, {
  key: 'left',
  area: 'lft',
  dir: 'LEFT'
}, {
  key: 'right',
  area: 'rgt',
  dir: 'RIGHT'
}, {
  key: 'back',
  area: 'bck',
  dir: 'BACK'
}];
function VisCrossroad({
  forward,
  left,
  right,
  back,
  center,
  roads = true,
  ...rest
}) {
  const routes = {
    forward,
    left,
    right,
    back
  };
  return /*#__PURE__*/React.createElement("div", _extends({
    className: "visuals-crossroad"
  }, rest), roads ? /*#__PURE__*/React.createElement("svg", {
    className: "visuals-crossroad-svg",
    viewBox: "0 0 100 100",
    preserveAspectRatio: "none",
    "aria-hidden": "true"
  }, /*#__PURE__*/React.createElement("path", {
    className: "visuals-road-edge",
    d: "M42 0V100M58 0V100M0 42H100M0 58H100"
  }), /*#__PURE__*/React.createElement("path", {
    className: "visuals-road-center",
    d: "M50 0V100M0 50H100"
  })) : null, VIS_AREAS.map(({
    key,
    area,
    dir
  }) => {
    const r = routes[key];
    if (!r) return null;
    return /*#__PURE__*/React.createElement("button", {
      key: key,
      type: "button",
      className: "visuals-crossroad-card",
      "data-area": area
    }, /*#__PURE__*/React.createElement("span", {
      className: "visuals-crossroad-dir"
    }, r.dir || dir), /*#__PURE__*/React.createElement("span", {
      className: "visuals-crossroad-name"
    }, r.name), r.hint ? /*#__PURE__*/React.createElement("span", {
      className: "visuals-crossroad-hint"
    }, r.hint) : null);
  }), /*#__PURE__*/React.createElement("div", {
    className: "visuals-crossroad-center"
  }, center));
}
Object.assign(__ds_scope, { VisCrossroad });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/visuals/VisCrossroad.jsx", error: String((e && e.message) || e) }); }

// components/visuals/VisGallery.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function visPinRot(i) {
  return [-3, 2.2, -1.5, 3.1, -2.4, 1.4][i % 6] + 'deg';
}
function visPinY(i) {
  return [0, 12, -8, 6, -5, 14][i % 6] + 'px';
}
function VisOverlay({
  icon = '\u26F6'
}) {
  return /*#__PURE__*/React.createElement("span", {
    className: "visuals-gallery-hover-overlay"
  }, /*#__PURE__*/React.createElement("span", {
    className: "visuals-gallery-hover-icon"
  }, icon));
}
function VisGallery({
  variant = 'museum',
  items = [],
  activeIndex = 0,
  ...rest
}) {
  if (variant === 'corridor') {
    const active = items[activeIndex] || items[0] || {};
    return /*#__PURE__*/React.createElement("div", _extends({
      className: "visuals-gallery-corridor"
    }, rest), /*#__PURE__*/React.createElement("div", {
      className: "visuals-corridor-stage"
    }, /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "visuals-corridor-arrow",
      "aria-label": "Previous"
    }, '\u2039'), /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "visuals-corridor-main"
    }, /*#__PURE__*/React.createElement("span", {
      className: "visuals-gallery-img-container",
      style: {
        display: 'flex'
      }
    }, active.src ? /*#__PURE__*/React.createElement("img", {
      src: active.src,
      alt: ""
    }) : null, /*#__PURE__*/React.createElement(VisOverlay, null))), /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "visuals-corridor-arrow",
      "aria-label": "Next"
    }, '\u203A')), /*#__PURE__*/React.createElement("div", {
      className: "visuals-corridor-caption"
    }, /*#__PURE__*/React.createElement("div", {
      className: "visuals-corridor-counter"
    }, activeIndex + 1, " / ", items.length), /*#__PURE__*/React.createElement("div", {
      className: "visuals-corridor-title"
    }, active.label)), /*#__PURE__*/React.createElement("div", {
      className: "visuals-corridor-strip"
    }, items.map((it, i) => /*#__PURE__*/React.createElement("button", {
      key: i,
      type: "button",
      className: 'visuals-corridor-thumb' + (i === activeIndex ? ' is-active' : '')
    }, it.src ? /*#__PURE__*/React.createElement("img", {
      src: it.src,
      alt: ""
    }) : null))));
  }
  if (variant === 'pinboard') {
    return /*#__PURE__*/React.createElement("div", _extends({
      className: "visuals-gallery-pinboard"
    }, rest), items.map((it, i) => /*#__PURE__*/React.createElement("button", {
      key: i,
      type: "button",
      className: "visuals-pinboard-card",
      style: {
        '--pin-rot': visPinRot(i),
        '--pin-y': visPinY(i)
      }
    }, /*#__PURE__*/React.createElement("span", {
      className: "visuals-pinboard-pin"
    }), /*#__PURE__*/React.createElement("span", {
      className: "visuals-pinboard-photo",
      style: {
        display: 'block'
      }
    }, it.src ? /*#__PURE__*/React.createElement("img", {
      src: it.src,
      alt: ""
    }) : null, /*#__PURE__*/React.createElement(VisOverlay, null)), /*#__PURE__*/React.createElement("span", {
      className: "visuals-pinboard-label",
      style: {
        display: 'block'
      }
    }, it.label))));
  }
  if (variant === 'pixel') {
    return /*#__PURE__*/React.createElement("div", _extends({
      className: "visuals-gallery-pixel"
    }, rest), items.map((it, i) => /*#__PURE__*/React.createElement("button", {
      key: i,
      type: "button",
      className: "visuals-pixel-cell"
    }, /*#__PURE__*/React.createElement("span", {
      className: "visuals-gallery-img-container is-pixel",
      style: {
        display: 'flex'
      }
    }, it.src ? /*#__PURE__*/React.createElement("img", {
      src: it.src,
      alt: ""
    }) : null, /*#__PURE__*/React.createElement(VisOverlay, null)), /*#__PURE__*/React.createElement("span", {
      className: "visuals-pixel-label",
      style: {
        display: 'block'
      }
    }, it.label))));
  }
  return /*#__PURE__*/React.createElement("div", _extends({
    className: "visuals-gallery-museum"
  }, rest), items.map((it, i) => /*#__PURE__*/React.createElement("button", {
    key: i,
    type: "button",
    className: "visuals-museum-frame"
  }, /*#__PURE__*/React.createElement("span", {
    className: "visuals-gallery-img-container",
    style: {
      display: 'flex'
    }
  }, it.src ? /*#__PURE__*/React.createElement("img", {
    src: it.src,
    alt: ""
  }) : null, /*#__PURE__*/React.createElement(VisOverlay, null)), /*#__PURE__*/React.createElement("span", {
    className: "visuals-museum-label",
    style: {
      display: 'block'
    }
  }, it.label))));
}
Object.assign(__ds_scope, { VisGallery });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/visuals/VisGallery.jsx", error: String((e && e.message) || e) }); }

// components/visuals/VisGalleryCard.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function VisGalleryCard({
  thumb,
  title,
  meta,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("button", _extends({
    type: "button",
    className: "visuals-gallery-card"
  }, rest), thumb ? /*#__PURE__*/React.createElement("img", {
    className: "visuals-gallery-card-thumb",
    src: thumb,
    alt: ""
  }) : /*#__PURE__*/React.createElement("span", {
    className: "visuals-gallery-card-thumb"
  }), /*#__PURE__*/React.createElement("span", {
    className: "visuals-gallery-card-body",
    style: {
      display: 'block'
    }
  }, /*#__PURE__*/React.createElement("span", {
    className: "visuals-gallery-card-title",
    style: {
      display: 'block'
    }
  }, title), meta ? /*#__PURE__*/React.createElement("span", {
    className: "visuals-gallery-card-meta",
    style: {
      display: 'block'
    }
  }, meta) : null));
}
Object.assign(__ds_scope, { VisGalleryCard });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/visuals/VisGalleryCard.jsx", error: String((e && e.message) || e) }); }

// components/visuals/VisLightbox.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function VisLightbox({
  src,
  caption,
  index,
  total,
  inline = false,
  closeLabel = 'CLOSE',
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    className: 'visuals-lightbox' + (inline ? ' visuals-lightbox--inline' : '')
  }, rest), /*#__PURE__*/React.createElement("div", {
    className: "visuals-lightbox-inner"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "visuals-lightbox-close"
  }, closeLabel), src ? /*#__PURE__*/React.createElement("img", {
    className: "visuals-lightbox-img",
    src: src,
    alt: ""
  }) : null, /*#__PURE__*/React.createElement("div", {
    className: "visuals-lightbox-meta"
  }, /*#__PURE__*/React.createElement("span", {
    className: "visuals-lightbox-caption"
  }, caption, index != null && total != null ? '  \u00B7  ' + index + ' / ' + total : ''), /*#__PURE__*/React.createElement("span", {
    className: "visuals-lightbox-nav"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "visuals-lightbox-btn"
  }, "Prev"), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "visuals-lightbox-btn"
  }, "Next")))));
}
Object.assign(__ds_scope, { VisLightbox });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/visuals/VisLightbox.jsx", error: String((e && e.message) || e) }); }

// components/visuals/VisSpriteViewer.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function VisSpriteViewer({
  animations = [],
  activeIndex = 0,
  frame = 1,
  frameCount = 8,
  playing = false,
  speed = '1x',
  speeds = ['0.5x', '1x', '2x'],
  sheet,
  frameSize = 96,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    className: "visuals-sprite-viewer"
  }, rest), /*#__PURE__*/React.createElement("div", {
    className: "visuals-sprite-anim-panel"
  }, /*#__PURE__*/React.createElement("div", {
    className: "visuals-sprite-anim-header"
  }, "Animations"), /*#__PURE__*/React.createElement("div", {
    className: "visuals-sprite-anim-list"
  }, animations.map((a, i) => /*#__PURE__*/React.createElement("button", {
    key: a.name || i,
    type: "button",
    className: 'visuals-sprite-anim-btn' + (i === activeIndex ? ' is-active' : '')
  }, /*#__PURE__*/React.createElement("span", {
    className: "visuals-sprite-anim-name"
  }, a.name), /*#__PURE__*/React.createElement("span", {
    className: "visuals-sprite-anim-range"
  }, a.range))))), /*#__PURE__*/React.createElement("div", {
    className: "visuals-sprite-display-panel"
  }, /*#__PURE__*/React.createElement("div", {
    className: "visuals-sprite-viewport-wrap"
  }, /*#__PURE__*/React.createElement("div", {
    className: "visuals-sprite-viewport",
    style: {
      width: frameSize,
      height: frameSize,
      backgroundImage: sheet ? 'url(' + sheet + ')' : undefined,
      backgroundSize: 'cover'
    }
  })), /*#__PURE__*/React.createElement("div", {
    className: "visuals-sprite-controls"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "visuals-sprite-play-btn"
  }, playing ? '\u2016' : '\u25B6'), /*#__PURE__*/React.createElement("div", {
    className: "visuals-sprite-speed-group"
  }, speeds.map(s => /*#__PURE__*/React.createElement("button", {
    key: s,
    type: "button",
    className: 'visuals-sprite-speed-btn' + (s === speed ? ' is-active' : '')
  }, s))), /*#__PURE__*/React.createElement("span", {
    className: "visuals-sprite-frame-counter"
  }, "FRAME ", frame, " / ", frameCount)), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "visuals-sprite-sheet-btn"
  }, "VIEW SPRITE SHEET")));
}
Object.assign(__ds_scope, { VisSpriteViewer });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/visuals/VisSpriteViewer.jsx", error: String((e && e.message) || e) }); }

// components/visuals/VisSubcatBoard.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function visRot(i) {
  return [-2.5, 1.8, -1.2, 2.6, -1.9, 1.1][i % 6] + 'deg';
}
function visOffset(i) {
  return [0, 10, -6, 8, -4, 12][i % 6] + 'px';
}
function VisSubcatBoard({
  variant = 'corridor',
  items = [],
  ...rest
}) {
  if (variant === 'museum') {
    return /*#__PURE__*/React.createElement("div", _extends({
      className: "visuals-div-museum"
    }, rest), items.map((it, i) => /*#__PURE__*/React.createElement("button", {
      key: it.id || i,
      type: "button",
      className: "visuals-div-museum-frame"
    }, /*#__PURE__*/React.createElement("span", {
      className: "visuals-div-museum-hook"
    }), /*#__PURE__*/React.createElement("span", {
      className: "visuals-div-museum-inner",
      style: {
        display: 'block'
      }
    }, /*#__PURE__*/React.createElement("span", {
      className: "visuals-div-museum-ornament",
      style: {
        display: 'block'
      }
    }, '\u2740'), /*#__PURE__*/React.createElement("span", {
      className: "visuals-div-museum-label",
      style: {
        display: 'block'
      }
    }, it.title), /*#__PURE__*/React.createElement("span", {
      className: "visuals-div-museum-divider",
      style: {
        display: 'block'
      }
    }), /*#__PURE__*/React.createElement("span", {
      className: "visuals-div-museum-count",
      style: {
        display: 'block'
      }
    }, it.count)))));
  }
  if (variant === 'pinboard') {
    return /*#__PURE__*/React.createElement("div", _extends({
      className: "visuals-div-pinboard"
    }, rest), items.map((it, i) => /*#__PURE__*/React.createElement("button", {
      key: it.id || i,
      type: "button",
      className: "visuals-div-pinboard-note",
      style: {
        '--note-rot': visRot(i),
        '--note-y': visOffset(i)
      }
    }, /*#__PURE__*/React.createElement("span", {
      className: "visuals-div-pinboard-pin"
    }), /*#__PURE__*/React.createElement("span", {
      className: "visuals-div-pinboard-title",
      style: {
        display: 'block'
      }
    }, it.title), /*#__PURE__*/React.createElement("span", {
      className: "visuals-div-pinboard-count",
      style: {
        display: 'block'
      }
    }, it.count))));
  }
  if (variant === 'gridpaper') {
    return /*#__PURE__*/React.createElement("div", _extends({
      className: "visuals-div-gridpaper"
    }, rest), /*#__PURE__*/React.createElement("div", {
      className: "visuals-div-gridpaper-cards"
    }, items.map((it, i) => /*#__PURE__*/React.createElement("button", {
      key: it.id || i,
      type: "button",
      className: "visuals-div-gridpaper-card"
    }, /*#__PURE__*/React.createElement("span", {
      className: "visuals-div-gridpaper-icon"
    }, it.icon || '\u25A6'), /*#__PURE__*/React.createElement("span", {
      className: "visuals-div-gridpaper-title"
    }, it.title), /*#__PURE__*/React.createElement("span", {
      className: "visuals-div-gridpaper-count"
    }, it.count)))));
  }
  return /*#__PURE__*/React.createElement("div", _extends({
    className: "visuals-div-corridor"
  }, rest), /*#__PURE__*/React.createElement("span", {
    className: "visuals-div-corridor-axis"
  }), items.map((it, i) => /*#__PURE__*/React.createElement("div", {
    key: it.id || i,
    className: 'visuals-div-corridor-slot visuals-div-corridor-slot--' + (i % 2 === 0 ? 'left' : 'right')
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "visuals-div-corridor-door"
  }, /*#__PURE__*/React.createElement("span", {
    className: "visuals-div-corridor-num"
  }, String(i + 1).padStart(2, '0')), /*#__PURE__*/React.createElement("span", {
    className: "visuals-div-corridor-title"
  }, it.title), /*#__PURE__*/React.createElement("span", {
    className: "visuals-div-corridor-meta"
  }, it.count)), /*#__PURE__*/React.createElement("span", {
    className: "visuals-div-corridor-connector"
  }))));
}
Object.assign(__ds_scope, { VisSubcatBoard });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/visuals/VisSubcatBoard.jsx", error: String((e && e.message) || e) }); }

// ui_kits/uep-docs/HomeScreen.jsx
try { (() => {
const {
  TopBar,
  MonoLabel,
  UepVoice,
  UepDialogue,
  Button,
  Hairline
} = window.UEPImaginarySpaceDesignSystem_6b2a32;
const homeStyles = {
  hero: {
    display: 'grid',
    gridTemplateColumns: '1.1fr 1fr',
    gap: 56,
    padding: '90px 64px',
    alignItems: 'center',
    maxWidth: 1400,
    margin: '0 auto',
    position: 'relative'
  },
  h1: {
    fontFamily: 'var(--font-display)',
    fontSize: 76,
    fontWeight: 500,
    lineHeight: 1.02,
    letterSpacing: '-.02em',
    color: 'var(--ink-title)',
    margin: '14px 0 0'
  },
  lede: {
    fontFamily: 'var(--font-serif-tc)',
    fontSize: 16,
    lineHeight: 1.9,
    color: 'var(--ink-soft)',
    fontStyle: 'italic',
    maxWidth: 520,
    margin: '26px 0 0'
  },
  atlas: {
    padding: '32px 64px 56px',
    background: 'var(--bg)'
  },
  legend: {
    display: 'grid',
    gridTemplateColumns: 'repeat(5,1fr)',
    borderTop: '1px solid var(--hairline)',
    maxWidth: 860,
    margin: '4px auto 0'
  },
  legendCell: {
    padding: '22px 14px',
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
    cursor: 'pointer',
    textAlign: 'left',
    background: 'transparent',
    border: 0,
    borderRight: '1px solid var(--hairline)',
    color: 'inherit'
  },
  verse: {
    padding: '80px 64px',
    display: 'flex',
    flexDirection: 'column',
    justifyContent: 'center',
    background: 'radial-gradient(ellipse 80% 50% at 50% 20%,color-mix(in srgb,var(--uep-gold) 7%,transparent),transparent 70%),var(--bg)'
  },
  verseText: {
    fontFamily: 'var(--font-serif-tc)',
    fontSize: 17,
    lineHeight: 2.08,
    color: 'var(--ink)',
    textAlign: 'center',
    maxWidth: 680,
    margin: '0 auto'
  },
  scene: {
    position: 'relative',
    padding: '80px 64px',
    overflow: 'hidden',
    background: 'var(--bg)'
  },
  sceneInner: {
    position: 'relative',
    zIndex: 1,
    display: 'grid',
    gridTemplateColumns: '300px minmax(0,1fr)',
    gap: 56,
    maxWidth: 1100,
    margin: '0 auto',
    alignItems: 'center'
  },
  sceneTitle: {
    fontFamily: 'var(--font-display)',
    fontSize: 52,
    fontWeight: 500,
    color: 'var(--ink-title)',
    margin: '0 0 16px',
    lineHeight: 1.1
  },
  narration: {
    fontFamily: 'var(--font-serif-tc)',
    fontSize: 15,
    lineHeight: 2,
    color: 'var(--ink)',
    margin: 0,
    maxWidth: 580
  },
  bridge: {
    padding: '40px 0',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center'
  }
};
function SectionBridge() {
  return /*#__PURE__*/React.createElement("div", {
    style: homeStyles.bridge
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      width: 1,
      height: 48,
      background: 'linear-gradient(to bottom,transparent,var(--uep-gold),transparent)',
      opacity: .35
    }
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      width: 4,
      height: 4,
      borderRadius: '50%',
      background: 'var(--uep-gold)',
      opacity: .5
    }
  }));
}
function JourneyScene({
  zone,
  onEnter
}) {
  return /*#__PURE__*/React.createElement("section", {
    "data-zone": zone.id,
    style: homeStyles.scene,
    "data-screen-label": 'Journey · ' + zone.en
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      position: 'absolute',
      inset: 0,
      zIndex: 0,
      pointerEvents: 'none',
      background: 'radial-gradient(ellipse 120% 90% at 8% 44%,color-mix(in srgb,var(--zone-main) 16%,transparent),transparent 62%),radial-gradient(ellipse 105% 110% at 92% 58%,color-mix(in srgb,var(--zone-soft) 11%,transparent),transparent 56%),linear-gradient(135deg,color-mix(in srgb,var(--zone-main) 4%,var(--bg)) 0%,var(--bg) 46%,color-mix(in srgb,var(--zone-soft) 5%,var(--bg)) 100%)'
    }
  }), /*#__PURE__*/React.createElement("div", {
    style: homeStyles.sceneInner
  }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement(MonoLabel, {
    tone: "zone"
  }, zone.kicker, " \xB7 ", zone.en), /*#__PURE__*/React.createElement("h2", {
    style: homeStyles.sceneTitle
  }, zone.label), /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      gap: 14,
      marginBottom: 24
    }
  }, zone.glyphs.map(g => /*#__PURE__*/React.createElement("span", {
    key: g,
    style: {
      fontFamily: 'var(--font-display)',
      fontSize: 24,
      color: 'var(--zone-main)',
      opacity: .7
    }
  }, g))), /*#__PURE__*/React.createElement("p", {
    style: {
      fontFamily: 'var(--font-serif-tc)',
      fontSize: 13,
      color: 'var(--ink-mute)',
      lineHeight: 1.8,
      marginBottom: 28
    }
  }, zone.blurb), /*#__PURE__*/React.createElement(Button, {
    variant: "zone",
    onClick: () => onEnter(zone.id)
  }, "\u9032\u5165 ", zone.en, " \u2192")), /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      gap: 28,
      alignItems: 'center'
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 18,
      flex: 1
    }
  }, /*#__PURE__*/React.createElement("p", {
    style: homeStyles.narration
  }, zone.uep[0]), zone.uep.slice(1).map((line, i) => /*#__PURE__*/React.createElement(UepDialogue, {
    key: i
  }, line))), /*#__PURE__*/React.createElement("img", {
    src: `../../assets/art/zone-${zone.id}.webp`,
    alt: "",
    style: {
      width: 180,
      alignSelf: 'flex-end',
      flexShrink: 0
    }
  }))), /*#__PURE__*/React.createElement("div", {
    style: {
      position: 'absolute',
      bottom: 28,
      left: 64,
      right: 64,
      display: 'flex',
      alignItems: 'center',
      gap: 16,
      zIndex: 1
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: 'var(--font-mono)',
      fontSize: 10,
      letterSpacing: '.16em',
      color: 'var(--ink-mute)'
    }
  }, zone.kicker), /*#__PURE__*/React.createElement("hr", {
    style: {
      flex: 1,
      border: 0,
      borderTop: '1px solid var(--zone-main)',
      opacity: .25,
      margin: 0
    }
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: 'var(--font-mono)',
      fontSize: 10,
      letterSpacing: '.16em',
      color: 'var(--ink-mute)'
    }
  }, zone.en.toUpperCase())));
}
function HomeScreen({
  theme,
  onToggleTheme,
  onEnterZone,
  onOpenMap,
  onOpenRitual
}) {
  return /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement(TopBar, {
    theme: theme,
    onToggleTheme: onToggleTheme,
    onOpenMap: onOpenMap,
    actions: onOpenRitual ? /*#__PURE__*/React.createElement(Button, {
      size: "sm",
      onClick: onOpenRitual
    }, "\u25CE \u89C0\u6E2C\u8005\u5354\u8B70") : null
  }), /*#__PURE__*/React.createElement("section", {
    style: homeStyles.hero,
    "data-screen-label": "Home \xB7 Hero"
  }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement(MonoLabel, {
    tone: "gold",
    track: "ritual"
  }, "U.E.P IMAGINARY SPACE"), /*#__PURE__*/React.createElement("h1", {
    style: homeStyles.h1
  }, "\u908A\u969B\u4E16\u754C", /*#__PURE__*/React.createElement("br", null), "\u89C0\u6E2C\u8A8C"), /*#__PURE__*/React.createElement("p", {
    style: homeStyles.lede
  }, "\u4E94\u500B\u5340\u57DF\uFF0C\u4E00\u689D\u53EF\u7D2F\u7A4D\u7684\u95B1\u8B80\u8EF8\u7DDA\u3002\u4F60\u8B80\u904E\u4EC0\u9EBC\u3001\u89E3\u9396\u4E86\u4EC0\u9EBC\uFF0C\u9019\u500B\u7AD9\u53F0\u90FD\u8A18\u5F97\u3002"), /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      gap: 12,
      marginTop: 32
    }
  }, /*#__PURE__*/React.createElement(Button, {
    variant: "gold",
    onClick: () => onEnterZone('history')
  }, "\u958B\u59CB\u95B1\u8B80"), /*#__PURE__*/React.createElement(Button, {
    onClick: onOpenMap
  }, "\u2726 \u5927\u5730\u5716"))), /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'grid',
      placeItems: 'center'
    }
  }, /*#__PURE__*/React.createElement("div", {
    className: "uep-halo"
  }, /*#__PURE__*/React.createElement("img", {
    className: "home-hero-portrait",
    src: "../../assets/uep/Big-UEP.webp",
    alt: "U.E.P",
    style: {
      width: 300
    }
  })))), /*#__PURE__*/React.createElement("section", {
    style: homeStyles.atlas,
    "data-screen-label": "Home \xB7 Atlas"
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      textAlign: 'center',
      marginBottom: 22
    }
  }, /*#__PURE__*/React.createElement(MonoLabel, {
    track: "crumb"
  }, "FIVE ZONES")), /*#__PURE__*/React.createElement("div", {
    style: homeStyles.legend
  }, window.ZONES.map((z, i) => /*#__PURE__*/React.createElement("button", {
    key: z.id,
    "data-zone": z.id,
    style: {
      ...homeStyles.legendCell,
      borderRight: i === 4 ? 0 : homeStyles.legendCell.borderRight
    },
    onClick: () => onEnterZone(z.id)
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: 'var(--font-mono)',
      fontSize: 10,
      letterSpacing: '.16em',
      color: 'var(--zone-main)'
    }
  }, z.kicker), /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: 'var(--font-display)',
      fontSize: 22,
      fontWeight: 600,
      color: 'var(--ink-title)'
    }
  }, z.label), /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: 'var(--font-mono)',
      fontSize: 10,
      letterSpacing: '.14em',
      textTransform: 'uppercase',
      color: 'var(--ink-mute)'
    }
  }, z.en))))), /*#__PURE__*/React.createElement(SectionBridge, null), window.ZONES.map(z => /*#__PURE__*/React.createElement(JourneyScene, {
    key: z.id,
    zone: z,
    onEnter: onEnterZone
  })), /*#__PURE__*/React.createElement("section", {
    style: homeStyles.verse,
    "data-screen-label": "Home \xB7 Verse"
  }, /*#__PURE__*/React.createElement("div", {
    style: homeStyles.verseText
  }, window.VERSES.map((line, i) => line === '—' ? /*#__PURE__*/React.createElement(Hairline, {
    key: i,
    variant: "gold"
  }) : /*#__PURE__*/React.createElement("div", {
    key: i
  }, line)))));
}
Object.assign(window, {
  HomeScreen,
  JourneyScene,
  SectionBridge,
  homeStyles
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/uep-docs/HomeScreen.jsx", error: String((e && e.message) || e) }); }

// ui_kits/uep-docs/ReaderScreen.jsx
try { (() => {
const {
  TopBar,
  NavTree,
  Breadcrumb,
  Prose,
  PrevNext,
  ChapterTimeline,
  UepDialogue,
  MonoLabel,
  Button,
  ZoneState
} = window.UEPImaginarySpaceDesignSystem_6b2a32;
const readerStyles = {
  shell: {
    height: '100%',
    display: 'flex',
    flexDirection: 'column',
    background: 'var(--bg)',
    color: 'var(--ink)',
    overflow: 'hidden'
  },
  main: {
    flex: 1,
    minHeight: 0,
    display: 'flex',
    position: 'relative',
    overflow: 'hidden'
  },
  sidebar: {
    width: 272,
    flex: '0 0 272px',
    display: 'flex',
    flexDirection: 'column',
    borderRight: '1px solid var(--line)',
    background: 'var(--bg-soft)',
    zIndex: 2
  },
  sidebarHead: {
    display: 'flex',
    justifyContent: 'space-between',
    gap: 16,
    padding: '22px 18px 16px',
    borderBottom: '1px solid var(--line)'
  },
  sidebarTitle: {
    margin: '5px 0 0',
    padding: 0,
    border: 0,
    background: 'transparent',
    fontFamily: 'var(--font-display)',
    fontSize: 26,
    fontWeight: 600,
    color: 'var(--ink-title)',
    lineHeight: 1.15,
    cursor: 'pointer',
    textAlign: 'left'
  },
  search: {
    display: 'block',
    padding: '14px 14px 12px',
    borderBottom: '1px solid var(--line)'
  },
  input: {
    width: '100%',
    border: '1px solid var(--line)',
    background: 'var(--bg-card)',
    color: 'var(--ink)',
    padding: '10px 11px',
    font: '13px var(--font-sans)',
    outline: 'none'
  },
  tree: {
    flex: 1,
    minHeight: 0,
    overflow: 'auto'
  },
  content: {
    flex: 1,
    minWidth: 0,
    overflowY: 'auto',
    position: 'relative',
    zIndex: 1
  },
  reading: {
    maxWidth: 940,
    margin: '0 auto',
    padding: '48px 56px 72px'
  },
  head: {
    paddingBottom: 28,
    marginBottom: 28,
    borderBottom: '1px solid var(--line)'
  },
  h2: {
    margin: '10px 0 0',
    fontFamily: 'var(--font-display)',
    fontSize: 42,
    fontWeight: 500,
    lineHeight: 1.05,
    color: 'var(--ink-title)'
  },
  lede: {
    margin: '22px 0 0',
    fontFamily: 'var(--font-serif-tc)',
    fontSize: 16,
    lineHeight: 1.9,
    color: 'var(--ink-soft)',
    fontStyle: 'italic',
    maxWidth: 620
  },
  marker: {
    position: 'absolute',
    right: 0,
    top: '38%',
    zIndex: 22,
    display: 'flex',
    flexDirection: 'row-reverse',
    alignItems: 'center',
    background: 'none',
    border: 0,
    padding: 0,
    cursor: 'pointer',
    transform: 'translateY(-50%)'
  }
};
function ScrollMarker({
  label = '上次讀到這裡'
}) {
  return /*#__PURE__*/React.createElement("button", {
    type: "button",
    style: readerStyles.marker
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      display: 'block',
      width: 88,
      height: 2,
      background: 'linear-gradient(90deg,var(--zone-main),transparent)',
      opacity: .7
    }
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      display: 'block',
      whiteSpace: 'nowrap',
      fontFamily: 'var(--font-mono)',
      fontSize: 11,
      letterSpacing: '.06em',
      color: 'var(--zone-main)',
      padding: '5px 12px',
      background: 'color-mix(in srgb,var(--zone-main) 10%,var(--bg-card))',
      border: '1px solid color-mix(in srgb,var(--zone-main) 45%,transparent)',
      opacity: .85
    }
  }, label));
}
function ReaderScreen({
  zone,
  theme,
  onToggleTheme,
  onHome,
  pageId,
  onSelectPage,
  onLocked,
  sidebarOpen = true,
  onToggleSidebar
}) {
  const page = window.PAGES[pageId];
  const flat = ['s1', 's2', 's5'];
  const idx = flat.indexOf(pageId);
  return /*#__PURE__*/React.createElement("div", {
    "data-zone": zone.id,
    style: readerStyles.shell,
    "data-screen-label": 'Reader · ' + (page ? page.title : zone.en)
  }, /*#__PURE__*/React.createElement(TopBar, {
    variant: "reader",
    title: zone.label,
    subtitle: zone.kicker + ' · ' + zone.en,
    theme: theme,
    onToggleTheme: onToggleTheme,
    actions: /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(Button, {
      variant: "terminal",
      onClick: onToggleSidebar
    }, sidebarOpen ? '收合目錄' : '展開目錄'), /*#__PURE__*/React.createElement(Button, {
      variant: "terminal",
      onClick: onHome
    }, "\u2190 \u56DE\u5927\u5EF3"))
  }), /*#__PURE__*/React.createElement("div", {
    style: readerStyles.main
  }, sidebarOpen ? /*#__PURE__*/React.createElement("aside", {
    style: readerStyles.sidebar
  }, /*#__PURE__*/React.createElement("div", {
    style: readerStyles.sidebarHead
  }, /*#__PURE__*/React.createElement("button", {
    style: readerStyles.sidebarTitle,
    onClick: onHome
  }, zone.label)), /*#__PURE__*/React.createElement("label", {
    style: readerStyles.search
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      display: 'block',
      marginBottom: 7,
      fontFamily: 'var(--font-mono)',
      fontSize: 10,
      letterSpacing: '.16em',
      textTransform: 'uppercase',
      color: 'var(--zone-main)'
    }
  }, "\u641C\u5C0B"), /*#__PURE__*/React.createElement("input", {
    style: readerStyles.input,
    placeholder: "\u7BC7\u540D\u6216\u95DC\u9375\u5B57"
  })), /*#__PURE__*/React.createElement("div", {
    style: readerStyles.tree
  }, /*#__PURE__*/React.createElement(NavTree, {
    items: window.HISTORY_TREE,
    currentId: pageId,
    onSelect: item => {
      if (item.lock && item.lock !== 'open') onLocked(item);else if (window.PAGES[item.id]) onSelectPage(item.id);
    }
  }))) : null, /*#__PURE__*/React.createElement("div", {
    style: readerStyles.content
  }, /*#__PURE__*/React.createElement(ScrollMarker, null), /*#__PURE__*/React.createElement("article", {
    style: readerStyles.reading,
    className: "history-page-transition"
  }, !page ? /*#__PURE__*/React.createElement(ZoneState, {
    state: "empty",
    large: true,
    message: "\u5F9E\u5DE6\u908A\u7684\u76EE\u9304\u9078\u4E00\u7BC7\u958B\u59CB\u3002"
  }) : /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("header", {
    style: readerStyles.head
  }, /*#__PURE__*/React.createElement(Breadcrumb, {
    items: [zone.en.toUpperCase(), '卷一', page.title]
  }), /*#__PURE__*/React.createElement("h2", {
    style: readerStyles.h2
  }, page.title), /*#__PURE__*/React.createElement("p", {
    style: readerStyles.lede
  }, page.lede)), /*#__PURE__*/React.createElement(Prose, null, page.body.map((p, i) => /*#__PURE__*/React.createElement("p", {
    key: i
  }, p)), /*#__PURE__*/React.createElement("blockquote", null, page.quote), /*#__PURE__*/React.createElement("h3", null, page.h3), page.body2.map((p, i) => /*#__PURE__*/React.createElement("p", {
    key: i
  }, p)), /*#__PURE__*/React.createElement("hr", null)), page.uep.map((line, i) => /*#__PURE__*/React.createElement(UepDialogue, {
    key: i
  }, line)), /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: 44
    }
  }, /*#__PURE__*/React.createElement(MonoLabel, {
    tone: "zone"
  }, "\u672C\u5377\u76EE\u9304"), /*#__PURE__*/React.createElement(ChapterTimeline, {
    currentId: pageId,
    items: [{
      id: 's1',
      title: 'Origin',
      desc: '最初的觀測',
      state: idx > 0 ? 'completed' : 'available'
    }, {
      id: 's2',
      title: 'Convergence',
      desc: '匯聚成個體',
      state: idx > 1 ? 'completed' : 'available'
    }, {
      id: 's3',
      title: 'Feedback',
      desc: '反饋為渾沌',
      state: 'progression'
    }, {
      id: 's4',
      title: '？？？',
      state: 'flag'
    }],
    onSelect: item => onSelectPage(item.id)
  })), /*#__PURE__*/React.createElement(PrevNext, {
    prev: idx > 0 ? window.PAGES[flat[idx - 1]].title : undefined,
    next: idx < flat.length - 1 ? window.PAGES[flat[idx + 1]].title : undefined,
    onPrev: () => onSelectPage(flat[idx - 1]),
    onNext: () => onSelectPage(flat[idx + 1])
  }))))));
}
Object.assign(window, {
  ReaderScreen,
  ScrollMarker,
  readerStyles
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/uep-docs/ReaderScreen.jsx", error: String((e && e.message) || e) }); }

// ui_kits/uep-docs/ZoneEntryScreen.jsx
try { (() => {
const {
  TopBar,
  MonoLabel,
  ArchCard,
  UepDialogue,
  Button,
  Breadcrumb
} = window.UEPImaginarySpaceDesignSystem_6b2a32;
const entryStyles = {
  root: {
    position: 'relative',
    minHeight: '100%'
  },
  content: {
    position: 'relative',
    zIndex: 1,
    margin: '0 auto',
    padding: '60px 40px 80px',
    maxWidth: 900
  },
  h1: {
    fontFamily: 'var(--font-display)',
    fontWeight: 500,
    color: 'var(--ink-title)',
    lineHeight: 1,
    letterSpacing: '-.02em',
    fontSize: 88,
    margin: '0 0 20px'
  },
  intro: {
    fontFamily: 'var(--font-serif-tc)',
    fontSize: 16,
    color: 'var(--ink-soft)',
    fontStyle: 'italic',
    lineHeight: 1.9,
    marginBottom: 36,
    maxWidth: 620
  },
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(3,minmax(0,1fr))',
    gap: 22,
    marginTop: 44
  }
};
function ZoneEntryScreen({
  zone,
  theme,
  onToggleTheme,
  onHome,
  onOpenChapter,
  onLocked
}) {
  return /*#__PURE__*/React.createElement("div", {
    "data-zone": zone.id,
    "data-screen-label": 'Zone Entry · ' + zone.en
  }, /*#__PURE__*/React.createElement(TopBar, {
    variant: "reader",
    title: zone.label,
    subtitle: zone.kicker + ' · ' + zone.en,
    theme: theme,
    onToggleTheme: onToggleTheme,
    actions: /*#__PURE__*/React.createElement(Button, {
      variant: "terminal",
      onClick: onHome
    }, "\u2190 \u56DE\u5927\u5EF3")
  }), /*#__PURE__*/React.createElement("div", {
    style: entryStyles.root
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      position: 'absolute',
      inset: 0,
      pointerEvents: 'none',
      overflow: 'hidden'
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      position: 'absolute',
      width: '80vw',
      height: '80vw',
      left: '-35vw',
      top: '-42vw',
      borderRadius: '50%',
      border: '1px solid color-mix(in srgb,var(--zone-atmo) 28%,transparent)'
    }
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      position: 'absolute',
      width: '62vw',
      height: '62vw',
      right: '-30vw',
      bottom: '-36vw',
      borderRadius: '50%',
      border: '1px dashed color-mix(in srgb,var(--zone-atmo) 28%,transparent)'
    }
  })), /*#__PURE__*/React.createElement("div", {
    style: entryStyles.content
  }, /*#__PURE__*/React.createElement(Breadcrumb, {
    items: [zone.en.toUpperCase(), '入口']
  }), /*#__PURE__*/React.createElement("h1", {
    style: entryStyles.h1
  }, zone.label), /*#__PURE__*/React.createElement("p", {
    style: entryStyles.intro
  }, zone.blurb), /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      gap: 14,
      marginBottom: 8
    }
  }, zone.glyphs.map(g => /*#__PURE__*/React.createElement("span", {
    key: g,
    style: {
      fontFamily: 'var(--font-display)',
      fontSize: 28,
      color: 'var(--zone-main)',
      opacity: .55
    }
  }, g))), /*#__PURE__*/React.createElement("div", {
    style: {
      margin: '32px 0',
      display: 'flex',
      flexDirection: 'column',
      gap: 14
    }
  }, zone.uep.map((line, i) => /*#__PURE__*/React.createElement(UepDialogue, {
    key: i
  }, line))), /*#__PURE__*/React.createElement(MonoLabel, {
    tone: "zone"
  }, "CHAPTERS"), /*#__PURE__*/React.createElement("div", {
    style: entryStyles.grid
  }, /*#__PURE__*/React.createElement(ArchCard, {
    index: "I",
    title: "\u8D77\u6E90",
    meta: "4 SECTIONS",
    onClick: () => onOpenChapter('c1')
  }), /*#__PURE__*/React.createElement(ArchCard, {
    index: "II",
    title: "\u6CD5\u5247",
    meta: "2 SECTIONS",
    onClick: () => onOpenChapter('c2')
  }), /*#__PURE__*/React.createElement(ArchCard, {
    index: "III",
    title: "\uFF1F\uFF1F\uFF1F",
    meta: "LOCKED",
    locked: true,
    onClick: onLocked
  })))));
}
Object.assign(window, {
  ZoneEntryScreen,
  entryStyles
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/uep-docs/ZoneEntryScreen.jsx", error: String((e && e.message) || e) }); }

// ui_kits/uep-docs/data.js
try { (() => {
const ZONES = [{
  id: 'history',
  label: '歷史典藏庫',
  en: 'History',
  kicker: 'Volume I',
  blurb: '小說、文章、篇章紀錄。書頁在無重力中編成書。',
  glyphs: ['史', '傳', '誌', '卷'],
  uep: ['很壯觀吧! 這些都是我在各個時空當中找到的故事，我很自豪喔!', '這些看起來像玻璃的東西叫做「回想碎片」，碰一下就會直接進到頭腦裡!', '那些咻咻咻飛來飛去的紙張，都在進行他們自己的「故事重導」喔!']
}, {
  id: 'echoes',
  label: '回音蒐藏間',
  en: 'Echoes',
  kicker: 'Volume II',
  blurb: '音樂、OST、聲音作品。可被捧起的回憶之球。',
  glyphs: ['音', '嗚', '迴', '響'],
  uep: ['歡迎來到充滿了世界之聲的蒐藏間，這裡聽到的全部都是實際存在的對話喔!', '這些球球叫做「回聲」，每一個都儲存著一段重要的回憶!', '藍 / 紅 / 綠 / 紫 — 每種顏色都對應到不同類型的故事，記得嗎?']
}, {
  id: 'visuals',
  label: '幻影重現室',
  en: 'Visuals',
  kicker: 'Volume III',
  blurb: '畫作、插圖、視覺作品。半透明的人物像在水面盪漾。',
  glyphs: ['影', '像', '幻', '鏡'],
  uep: ['小心不要跟錯人了喔，小U.E.P可是獨一無二的!', '這裡是世界的印象，每一個人都曾經存在於某一個時間當中。', '他們是虛假幻象，但你是可以去接觸甚至仔細觀察他們的喔!']
}, {
  id: 'concepts',
  label: '概念調整房',
  en: 'Concepts',
  kicker: 'Volume IV',
  blurb: '世界觀、設定文件。原質、概念、伺服器內部。',
  glyphs: ['定', '質', '規', '理'],
  uep: ['很科幻的房間對不對! 而且還很大 (回音: 大大大大大....)', '所有關於世界的概念全部都在這裡! 他們會自己去修復錯誤並逐漸變得完美!', '這些東西看起來像是文字，但實際上是「原質」(Essence) 喔!']
}, {
  id: 'storage',
  label: '某人的置物空間',
  en: 'Storage',
  kicker: 'Volume V',
  blurb: '公告、Meta、雜項。一片散亂卻自有秩序的房間。',
  glyphs: ['記', '雜', '稿', 'Σ'],
  uep: ['這裡...這裡是哪裡啊? 我不記得這裡有這種房間啊?', '這個空間像是某個人的倉庫? 不知道是不是被其他力量所干涉而產生的。', '如果你對於這裡有些興趣的話，之後應該可以帶你回來的!']
}];
const VERSES = ['萬物由最原初的質所成', '匯聚成個體', '組合成群體', '在有限的時間中無限的擴展著', '—', '法則隨秩序而生', '反饋為渾沌，卻從未曾喪失其中的平衡', '隨即，一條搭載著眾個體命運的宇宙被觀測', '概念從其中迸發，逐漸構造出世界的框架', '—', '終點與起點本是個環', '創世將存在賦予給個體', '毀滅將存在自個體之中奪去', '起點跟終點本是個環'];
const HISTORY_TREE = [{
  id: 'z',
  kind: 'ZONE',
  title: '歷史典藏庫',
  children: [{
    id: 'c1',
    kind: 'CHAP',
    title: '卷一 · 起源',
    children: [{
      id: 's1',
      kind: 'SECT',
      title: '最初的觀測'
    }, {
      id: 's2',
      kind: 'SECT',
      title: '匯聚成個體'
    }, {
      id: 's3',
      kind: 'SECT',
      title: '反饋為渾沌',
      lock: 'progression'
    }, {
      id: 's4',
      kind: 'SECT',
      title: '隱藏的一節',
      lock: 'flag'
    }]
  }, {
    id: 'c2',
    kind: 'CHAP',
    title: '卷二 · 法則',
    children: [{
      id: 's5',
      kind: 'SECT',
      title: '秩序與渾沌'
    }, {
      id: 's6',
      kind: 'SECT',
      title: '觀測者的位置',
      lock: 'progression'
    }]
  }, {
    id: 'c3',
    kind: 'CHAP',
    title: '卷三 · ？？？',
    lock: 'flag'
  }]
}];
const PAGES = {
  s1: {
    title: '最初的觀測',
    lede: '在還沒有名字的時候，這個世界只是一次被記錄下來的偶然。',
    body: ['萬物由最原初的質所成，匯聚成個體，組合成群體，在有限的時間中無限的擴展著。', '那時沒有觀測者。沒有人替它命名，也沒有人記下它膨脹的速度——只有質本身在移動。'],
    quote: '終點與起點本是個環',
    h3: '觀測記錄',
    body2: ['法則隨秩序而生，反饋為渾沌，卻從未曾喪失其中的平衡。', '隨即，一條搭載著眾個體命運的宇宙被觀測。概念從其中迸發，逐漸構造出世界的框架。'],
    uep: ['這一段是我第一次找到的紀錄喔! 雖然有點模糊，但它確實在那裡!']
  },
  s2: {
    title: '匯聚成個體',
    lede: '質開始選擇彼此。這一節記錄的是選擇本身，而不是結果。',
    body: ['個體不是被創造的，是被留下的。那些沒有匯聚的質仍然在那裡，只是不再有人替它們記錄。'],
    quote: '聚合 跟 反饋 相輔相成',
    h3: '殘留',
    body2: ['創世 和 毀滅 本為同根。輪迴 與 置換 相互平衡，而萬物的終焉將歸約於 — 虛無。'],
    uep: ['讀到這裡你應該已經拿到第一個印記了! 繼續往下捲吧!']
  },
  s5: {
    title: '秩序與渾沌',
    lede: '法則不是規定，是觀測次數足夠多之後留下的形狀。',
    body: ['渾沌不是秩序的反面。它是秩序還沒有被觀測到的部分。'],
    quote: '反饋為渾沌，卻從未曾喪失其中的平衡',
    h3: '第二次觀測',
    body2: ['當同一條宇宙被觀測第二次，概念就不再需要迸發——它已經在那裡等著了。'],
    uep: ['這一卷比較難懂，如果卡住了，可以去概念調整房找對應的原質!']
  }
};
Object.assign(window, {
  ZONES,
  VERSES,
  HISTORY_TREE,
  PAGES
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/uep-docs/data.js", error: String((e && e.message) || e) }); }

__ds_ns.ArchCard = __ds_scope.ArchCard;

__ds_ns.ChapterTimeline = __ds_scope.ChapterTimeline;

__ds_ns.NoteSheet = __ds_scope.NoteSheet;

__ds_ns.Prose = __ds_scope.Prose;

__ds_ns.BrandMark = __ds_scope.BrandMark;

__ds_ns.Button = __ds_scope.Button;

__ds_ns.Hairline = __ds_scope.Hairline;

__ds_ns.MonoLabel = __ds_scope.MonoLabel;

__ds_ns.UepDialogue = __ds_scope.UepDialogue;

__ds_ns.UepVoice = __ds_scope.UepVoice;

__ds_ns.EchoAppreciation = __ds_scope.EchoAppreciation;

__ds_ns.EchoOrbCard = __ds_scope.EchoOrbCard;

__ds_ns.EchoPlayer = __ds_scope.EchoPlayer;

__ds_ns.EchoPlaylist = __ds_scope.EchoPlaylist;

__ds_ns.EchoSubcatRow = __ds_scope.EchoSubcatRow;

__ds_ns.EchoVinyl = __ds_scope.EchoVinyl;

__ds_ns.Dialog = __ds_scope.Dialog;

__ds_ns.RitualPanel = __ds_scope.RitualPanel;

__ds_ns.Toast = __ds_scope.Toast;

__ds_ns.ToastStack = __ds_scope.ToastStack;

__ds_ns.ZoneState = __ds_scope.ZoneState;

__ds_ns.Breadcrumb = __ds_scope.Breadcrumb;

__ds_ns.NavTree = __ds_scope.NavTree;

__ds_ns.PrevNext = __ds_scope.PrevNext;

__ds_ns.TopBar = __ds_scope.TopBar;

__ds_ns.VisCrossroad = __ds_scope.VisCrossroad;

__ds_ns.VisGallery = __ds_scope.VisGallery;

__ds_ns.VisGalleryCard = __ds_scope.VisGalleryCard;

__ds_ns.VisLightbox = __ds_scope.VisLightbox;

__ds_ns.VisSpriteViewer = __ds_scope.VisSpriteViewer;

__ds_ns.VisSubcatBoard = __ds_scope.VisSubcatBoard;

})();
