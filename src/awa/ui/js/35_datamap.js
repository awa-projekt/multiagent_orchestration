/* Where the data comes from: every source with its state (live, a copy of your file, a stand-in,
   the results folder) and, per agent, what it reads and writes. Shown after the build, on Review
   and on Connect; the exact technical names stay in a Details fold. */
const SOURCE_STATE = {
  live: "Live", copy: "Your file (copy)", "stand-in": "Stand-in", output: "Results folder",
};

function dataMapView(map, options) {
  const opts = options || {};
  if (!map || !map.sources || !map.sources.length) {
    return h("p", {class: "hint", text: "The agents use no data source: they work on what each request carries."});
  }
  const sources = map.sources.map((s) => h("div", {class: "card map-source"},
    h("h3", {}, s.name, " ", h("span", {class: "hint", text: "(" + s.kind + ")"})),
    h("span", {class: "state " + s.state, text: SOURCE_STATE[s.state] || s.state}),
    h("p", {text: s.sentence.charAt(0).toUpperCase() + s.sentence.slice(1)}),
    s.used_by.length ? h("p", {class: "hint", text: "Used by: " + s.used_by.join(", ")}) : null,
    s.state === "stand-in" && opts.connect ? h("p", {}, h("a", {href: "#connect", text: "Connect " + s.name})) : null,
    h("details", {}, h("summary", {text: "Details"}), h("ul", {}, s.details.map((d) => h("li", {}, h("code", {text: d})))))));
  const rows = map.agents.map((a) => {
    const reads = a.uses.filter((u) => u.access === "reads"), writes = a.uses.filter((u) => u.access === "writes");
    const cell = (uses) => uses.length ? uses.map((u) => h("div", {},
      u.source, " ", h("span", {class: "hint", text: "(" + u.tool + (u.approval ? ", asks a person first" : "") + ")"}))) : h("span", {class: "hint", text: "nothing"});
    return h("tr", {}, h("th", {scope: "row", text: a.name}), h("td", {}, cell(reads)), h("td", {}, cell(writes)));
  });
  return h("div", {class: "data-map"},
    h("div", {}, sources),
    h("div", {class: "scroll"}, h("table", {class: "map-agents"},
      h("caption", {class: "hint", text: "What each agent reads and writes, and through which tool"}),
      h("thead", {}, h("tr", {}, ["Agent", "Reads", "Writes"].map((c) => h("th", {scope: "col", text: c})))),
      h("tbody", {}, rows))));
}
