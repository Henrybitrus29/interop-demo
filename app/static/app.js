(function () {
  'use strict';

  // ---------------------------------------------------------------- small helpers

  function h(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'class') node.className = attrs[k];
      else if (k === 'text') node.textContent = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) {
      if (c == null) return;
      node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  var wakeTimer = null;
  function showWake(on) { document.getElementById('wake').hidden = !on; }

  // ---------------------------------------------------------------- the two demos

  function splitLines(text) { return text.split(/\r\n|\r|\n/); }

  function editMsh(text, index, value) {
    var lines = splitLines(text);
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].indexOf('MSH') === 0) {
        var parts = lines[i].split('|');
        if (parts.length <= index) return null;
        parts[index] = value;
        lines[i] = parts.join('|');
        return lines.join('\n');
      }
    }
    return null;
  }

  function editJson(text, mutate) {
    var obj;
    try { obj = JSON.parse(text); } catch (e) { return null; }
    if (!obj || typeof obj !== 'object') return null;
    try { mutate(obj); } catch (e) { return null; }
    return JSON.stringify(obj, null, 2);
  }

  var TABS = {
    hl7: {
      endpoint: '/api/hl7',
      field: 'message',
      inputLabel: 'HL7 v2 message',
      intro: 'A hospital lab system sends results as HL7 v2, a pipe-delimited text format from the 1980s. The bridge converts each message to FHIR R4 and checks it before anything is delivered.',
      outputs: [{ id: 'bundle', label: 'FHIR bundle' }, { id: 'ack', label: 'Reply to sender' }],
      edits: [
        { label: 'Mark it as test traffic', apply: function (t) { return editMsh(t, 10, 'T'); } },
        { label: 'Remove the patient ID', apply: function (t) {
            var lines = splitLines(t), done = false;
            lines = lines.map(function (l) {
              if (!done && l.indexOf('PID|') === 0) { var p = l.split('|'); if (p.length > 3) { p[3] = ''; done = true; } return p.join('|'); }
              return l;
            });
            return done ? lines.join('\n') : null;
          } },
        { label: 'Make it an admission message', apply: function (t) { return editMsh(t, 8, 'ADT^A01^ADT_A01'); } }
      ]
    },
    invoice: {
      endpoint: '/api/invoice',
      field: 'invoice',
      inputLabel: 'Invoice as JSON',
      intro: 'An accounting system exports plain invoice data. The converter builds the UBL XML that Peppol networks require, then checks it against the official EN 16931 rules before anything is sent.',
      outputs: [{ id: 'xml', label: 'UBL XML' }],
      edits: [
        { label: 'Remove the seller', apply: function (t) { return editJson(t, function (o) { delete o.seller; }); } },
        { label: 'Bill 1,000 kWh at 0.0125', apply: function (t) {
            return editJson(t, function (o) { var l = o.lines[0]; l.quantity = '1000'; l.unit_price = '0.0125'; l.unit_code = 'KWH'; });
          } },
        { label: 'Give the price five decimals', apply: function (t) { return editJson(t, function (o) { o.lines[0].unit_price = '0.12345'; }); } },
        { label: 'Use VAT category X', apply: function (t) { return editJson(t, function (o) { o.lines[0].vat_category = 'X'; }); } }
      ]
    }
  };

  var samples = null;
  var panels = {};

  // ---------------------------------------------------------------- building a panel

  function buildPanel(key) {
    var cfg = TABS[key];
    var root = document.getElementById('panel-' + key);
    var P = { key: key, cfg: cfg, root: root, run: 0, outputId: null, lastResult: null };
    panels[key] = P;

    var select = h('select', { id: 'sample-' + key });
    [['accepted', 'Should be accepted'], ['rejected', 'Rejected on purpose']].forEach(function (g) {
      var group = h('optgroup', { label: g[1] });
      samples[key].forEach(function (s) {
        if (s.expect === g[0]) group.appendChild(h('option', { value: s.id, text: s.label }));
      });
      select.appendChild(group);
    });

    P.note = h('p', { class: 'sample-note', id: 'note-' + key });
    P.input = h('textarea', { id: 'input-' + key, spellcheck: 'false', autocapitalize: 'off', autocomplete: 'off', 'aria-label': cfg.inputLabel });
    P.convert = h('button', { type: 'button', class: 'primary', text: 'Convert' });
    var reset = h('button', { type: 'button', class: 'quiet', text: 'Reset sample' });
    P.editNote = h('p', { class: 'edit-note', role: 'status' });

    var chips = h('div', { class: 'chips' });
    cfg.edits.forEach(function (e) {
      var b = h('button', { type: 'button', class: 'chip', text: e.label });
      b.addEventListener('click', function () {
        var next = e.apply(P.input.value);
        if (next == null) { P.editNote.textContent = 'That change needs a valid ' + (key === 'hl7' ? 'HL7 message' : 'invoice') + ' to work on. Reset the sample and try again.'; return; }
        P.editNote.textContent = '';
        P.input.value = next;
        convert(P);
      });
      chips.appendChild(b);
    });

    var source = h('div', { class: 'source' }, [
      h('div', { class: 'field-head' }, [h('label', { for: 'sample-' + key, text: 'Sample' }), select]),
      P.note,
      P.input,
      h('div', { class: 'actions' }, [P.convert, reset, h('span', { class: 'hint', text: 'Ctrl+Enter also converts' })]),
      h('div', { class: 'edits' }, [h('h2', { text: 'Try a change' }), chips, P.editNote])
    ]);

    P.result = h('div', { class: 'result', 'aria-live': 'polite' });
    root.appendChild(h('p', { class: 'intro', text: cfg.intro }));
    root.appendChild(h('div', { class: 'bench' }, [source, P.result]));

    function load(id) {
      var s = samples[key].filter(function (x) { return x.id === id; })[0];
      if (!s) return;
      P.sample = s;
      P.input.value = s.text;
      P.note.textContent = s.note;
      P.editNote.textContent = '';
      select.value = id;
      convert(P);
    }
    select.addEventListener('change', function () { load(select.value); });
    reset.addEventListener('click', function () { load(select.value); });
    P.convert.addEventListener('click', function () { convert(P); });
    P.input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); convert(P); }
    });

    load(samples[key][0].id);
  }

  // ---------------------------------------------------------------- calling the server

  function convert(P) {
    var token = ++P.run;
    var body = {};
    body[P.cfg.field] = P.input.value;
    P.convert.disabled = true;
    clear(P.result);
    P.result.appendChild(h('p', { class: 'pending', text: 'Converting...' }));
    clearTimeout(wakeTimer);
    wakeTimer = setTimeout(function () { showWake(true); }, 2500);

    fetch(P.cfg.endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) { return { status: res.status, data: data }; });
      })
      .then(function (r) {
        if (token !== P.run) return;
        if (r.status !== 200 || !r.data.rail) {
          renderProblem(P, r.data && r.data.error ? r.data.error : 'The server answered with an error (' + r.status + '). Try again in a moment.');
        } else {
          P.lastResult = r.data;
          renderResult(P, r.data);
        }
      })
      .catch(function () {
        if (token !== P.run) return;
        renderProblem(P, 'The converter could not be reached. Check your connection and try again.');
      })
      .then(function () {
        if (token !== P.run) return;
        clearTimeout(wakeTimer);
        showWake(false);
        P.convert.disabled = false;
      });
  }

  // ---------------------------------------------------------------- drawing a result

  function renderProblem(P, message) {
    clear(P.result);
    P.result.appendChild(h('div', { class: 'verdict', 'data-tone': 'bad' }, [
      h('p', { class: 'verdict-title', text: 'Could not convert' }),
      h('p', { class: 'verdict-body', text: message })
    ]));
  }

  function verdictFor(kind, r) {
    var warnings = r.issues.filter(function (i) { return i.severity === 'warning'; }).length;
    var failed = r.rail.filter(function (s) { return s.state === 'fail'; })[0];
    if (r.ok && r.validated) {
      var body = kind === 'hl7'
        ? 'Converted to FHIR and passed validation. The sender would receive an AA acknowledgement.'
        : 'Built as UBL 2.1 and passed every EN 16931 rule.';
      if (warnings) body += ' It carries ' + plural(warnings, 'warning') + ', listed under Checks.';
      return { tone: 'ok', title: kind === 'hl7' ? 'Accepted' : 'Valid invoice', body: body };
    }
    if (r.ok) {
      return { tone: 'warn', title: 'Converted, not validated',
        body: 'This server has no ' + (kind === 'hl7' ? 'FHIR validator' : 'EN 16931 rule set') + ' installed, so the output was not checked.' };
    }
    var stage = failed ? failed.label.toLowerCase() : 'an earlier stage';
    var tail = kind === 'hl7'
      ? ' The sender would receive an AR acknowledgement, and the message would wait in the dead-letter store for a person to review.'
      : (failed && failed.id === 'map' ? ' No XML was produced.' : ' The invoice would wait in the dead-letter store instead of being sent.');
    var reason = /[.!?]$/.test(r.reason) ? r.reason : r.reason + '.';
    return { tone: 'bad', title: 'Rejected at ' + stage, body: reason + tail };
  }

  function renderRail(rail) {
    var list = h('ol', { class: 'rail enter', 'aria-label': 'Pipeline stages' });
    rail.forEach(function (s, i) {
      var glyph = { pass: '✓', fail: '✕', skipped: '–', notreached: '' }[s.state];
      var word = { pass: 'Passed', fail: 'Failed', skipped: 'Skipped', notreached: 'Not reached' }[s.state];
      var node = h('span', { class: 'node', 'aria-hidden': 'true', text: glyph });
      node.style.setProperty('--i', String(i));
      list.appendChild(h('li', { 'data-state': s.state }, [
        node,
        h('span', { class: 'rail-label', text: s.label }),
        h('span', { class: 'rail-note', text: (s.note ? s.note : word) }),
        h('span', { class: 'visually-hidden', text: ' ' + word })
      ]));
    });
    return list;
  }

  function table(headers, rows, numericCols) {
    var head = h('tr', {}, headers.map(function (t, i) { return h('th', { class: numericCols.indexOf(i) >= 0 ? 'num' : '', scope: 'col', text: t }); }));
    var body = rows.map(function (cells) {
      return h('tr', {}, cells.map(function (c, i) {
        var cls = numericCols.indexOf(i) >= 0 ? 'num' : '';
        if (c && typeof c === 'object') { cls += (cls ? ' ' : '') + c.cls; c = c.text; }
        return h('td', { class: cls, text: c });
      }));
    });
    return h('div', { class: 'table-scroll' }, [h('table', {}, [h('thead', {}, [head]), h('tbody', {}, body)])]);
  }

  function renderSummary(kind, summary) {
    if (!summary) return null;
    var box = h('section');
    if (kind === 'hl7') {
      var counts = Object.keys(summary.counts || {}).map(function (k) { return plural(summary.counts[k], k); }).join(', ');
      box.appendChild(h('h2', { class: 'block', text: 'What was extracted' }));
      if (counts) box.appendChild(h('p', { class: 'counts', text: counts }));
      if (summary.observations.length) {
        box.appendChild(table(['Test', 'Result', 'Flag', 'Status'], summary.observations.map(function (o) {
          return [o.test, o.result, o.flag ? { text: o.flag, cls: 'flag-strong' } : '', o.status];
        }), []));
      }
      return box;
    }
    box.appendChild(h('h2', { class: 'block', text: summary.document + ' ' + summary.number + ' in ' + summary.currency }));
    box.appendChild(table(['Item', 'Quantity', 'Unit price', 'VAT', 'Line total'], summary.lines.map(function (l) {
      return [l.name, (l.quantity + ' ' + l.unit).trim(), l.unit_price, l.vat, l.net];
    }), [1, 2, 4]));
    var t = summary.totals || {};
    box.appendChild(h('dl', { class: 'totals' }, [
      h('dt', { text: 'Net total' }), h('dd', { text: t.net || '' }),
      h('dt', { text: 'VAT' }), h('dd', { text: t.vat || '' }),
      h('dt', { class: 'grand', text: 'Payable' }), h('dd', { class: 'grand', text: t.payable || '' })
    ]));
    return box;
  }

  function renderChecks(issues) {
    if (!issues.length) {
      return h('section', {}, [h('h2', { class: 'block', text: 'Checks' }), h('p', { class: 'none', text: 'No warnings or errors.' })]);
    }
    var errors = issues.filter(function (i) { return i.severity === 'error'; }).length;
    var warnings = issues.length - errors;
    var label = [errors ? plural(errors, 'error') : '', warnings ? plural(warnings, 'warning') : ''].filter(Boolean).join(' and ');
    var ul = h('ul', { class: 'checks' });
    var ordered = issues.slice().sort(function (a, b) { return (a.severity === 'error' ? 0 : 1) - (b.severity === 'error' ? 0 : 1); });
    ordered.forEach(function (i) {
      ul.appendChild(h('li', {}, [
        h('span', { class: 'sev', 'data-sev': i.severity, text: i.severity === 'error' ? 'Error' : 'Warning' }),
        i.where ? h('span', { class: 'where', text: i.where }) : null,
        document.createTextNode(i.message)
      ]));
    });
    var box = h('details', { class: 'checks-box' }, [h('summary', { text: 'Checks: ' + label }), ul]);
    if (errors || issues.length <= 3) box.open = true;      // errors and short lists stay open; long warning lists collapse
    return box;
  }

  function renderOutputs(P, r) {
    var box = h('section');
    var outputs = P.cfg.outputs;
    var available = outputs.filter(function (o) { return r.outputs[o.id]; });
    if (!outputs.some(function (o) { return o.id === P.outputId; })) P.outputId = null;
    var current = P.outputId && r.outputs[P.outputId] ? P.outputId : (available[0] || outputs[0]).id;

    var tabs = h('div', { class: 'out-tabs', role: 'tablist', 'aria-label': 'Output' });
    var pre = h('pre', { class: 'code', tabindex: '0' });
    var copy = h('button', { type: 'button', class: 'copy', text: 'Copy' });
    var buttons = {};

    function show(id) {
      P.outputId = id;
      var text = r.outputs[id];
      pre.textContent = text || 'Nothing was produced: processing stopped before this stage.';
      copy.disabled = !text;
      Object.keys(buttons).forEach(function (k) {
        buttons[k].setAttribute('aria-selected', String(k === id));
        buttons[k].tabIndex = k === id ? 0 : -1;
      });
    }
    outputs.forEach(function (o) {
      var b = h('button', { type: 'button', role: 'tab', text: o.label });
      b.addEventListener('click', function () { show(o.id); });
      buttons[o.id] = b;
      tabs.appendChild(b);
    });
    copy.addEventListener('click', function () {
      var text = r.outputs[P.outputId] || '';
      var done = function () { copy.textContent = 'Copied'; setTimeout(function () { copy.textContent = 'Copy'; }, 1500); };
      try { navigator.clipboard.writeText(text).then(done, function () {}); } catch (e) { /* clipboard unavailable */ }
    });

    box.appendChild(h('div', { class: 'out-bar' }, [tabs, copy]));
    box.appendChild(pre);
    show(current);
    return box;
  }

  function renderResult(P, r) {
    var v = verdictFor(P.key, r);
    clear(P.result);
    P.result.appendChild(h('div', { class: 'verdict', 'data-tone': v.tone }, [
      h('p', { class: 'verdict-title', text: v.title }),
      h('p', { class: 'verdict-body', text: v.body })
    ]));
    P.result.appendChild(renderRail(r.rail));
    var summary = renderSummary(P.key, r.summary);
    if (summary) P.result.appendChild(summary);
    if (r.ok || r.issues.length) P.result.appendChild(renderChecks(r.issues));      // a rejection's reason is already in the verdict
    var produced = P.cfg.outputs.some(function (o) { return r.outputs[o.id]; });
    if (produced) P.result.appendChild(renderOutputs(P, r));
  }

  // ---------------------------------------------------------------- tabs

  function selectTab(key, focus) {
    Object.keys(TABS).forEach(function (k) {
      var on = k === key;
      var tab = document.getElementById('tab-' + k);
      tab.setAttribute('aria-selected', String(on));
      tab.tabIndex = on ? 0 : -1;
      document.getElementById('panel-' + k).hidden = !on;
      if (on && focus) tab.focus();
    });
    try { history.replaceState(null, '', '#' + key); } catch (e) { /* ignore */ }
  }

  function wireTabs() {
    var keys = Object.keys(TABS);
    keys.forEach(function (k) {
      var tab = document.getElementById('tab-' + k);
      tab.addEventListener('click', function () { selectTab(k, false); });
      tab.addEventListener('keydown', function (ev) {
        var i = keys.indexOf(k), next = null;
        if (ev.key === 'ArrowRight') next = keys[(i + 1) % keys.length];
        else if (ev.key === 'ArrowLeft') next = keys[(i + keys.length - 1) % keys.length];
        if (next) { ev.preventDefault(); selectTab(next, true); }
      });
    });
    var wanted = (location.hash || '').replace('#', '');
    selectTab(TABS[wanted] ? wanted : 'hl7', false);
  }

  // ---------------------------------------------------------------- start

  function loadSamples(attempt) {
    fetch('/api/samples')
      .then(function (r) { if (!r.ok) throw new Error('bad status'); return r.json(); })
      .then(function (data) {
        showWake(false);
        samples = data;
        Object.keys(TABS).forEach(buildPanel);
      })
      .catch(function () {
        if (attempt < 6) setTimeout(function () { loadSamples(attempt + 1); }, 4000);
        else {
          showWake(false);
          document.getElementById('panel-hl7').appendChild(h('p', { class: 'notice', text: 'The demo could not load. Reload the page to try again.' }));
        }
      });
    setTimeout(function () { if (!samples) showWake(true); }, 2500);
  }

  function loadConfig() {
    fetch('/static/config.json').then(function (r) { return r.ok ? r.json() : null; }).then(function (cfg) {
      if (!cfg || !cfg.links) return;
      var links = cfg.links.filter(function (l) { return l.url; });
      if (!links.length) return;
      var box = document.getElementById('links');
      links.forEach(function (l) { box.appendChild(h('a', { href: l.url, rel: 'noopener', text: l.label })); });
      box.hidden = false;
    }).catch(function () { /* optional */ });
  }

  wireTabs();
  loadSamples(0);
  loadConfig();
})();
