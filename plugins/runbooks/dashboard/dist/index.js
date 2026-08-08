(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const { React } = SDK;
  const h = React.createElement;
  const { useEffect, useMemo, useState } = SDK.hooks;
  const {
    Badge, Button, Card, CardContent, Input, Label, Select, SelectOption,
  } = SDK.components;

  const API = "/api/plugins/runbooks";

  function fetchJSON(path, options) {
    return fetch(API + path, Object.assign({
      headers: { "Content-Type": "application/json" },
    }, options || {})).then(async function (response) {
      const text = await response.text();
      const data = text ? JSON.parse(text) : {};
      if (!response.ok) {
        const detail = data && data.detail ? data.detail : text;
        throw new Error(detail || response.statusText);
      }
      return data;
    });
  }

  function emptyMarkdown(slug) {
    return [
      "---",
      "id: wf_" + slug.replace(/[^a-zA-Z0-9_]/g, "_"),
      "slug: " + slug,
      "title: New Runbook",
      "purpose: Describe the workflow outcome.",
      "owner_profile: root",
      "status: draft",
      "runtime:",
      "  kind: hermes",
      "  ref: null",
      "schedules: []",
      "steps:",
      "  - step_key: prepare",
      "    name: Prepare",
      "    executor_profile: root",
      "inputs: {}",
      "outputs: {}",
      "permitted_writes: []",
      "approval_rules: {}",
      "retry:",
      "  max_attempts: 1",
      "timeout:",
      "  seconds: 3600",
      "deduplication:",
      "  strategy: manual",
      "related: {}",
      "---",
      "# New Runbook",
      "",
      "## Procedure",
      "",
      "1. Prepare the workflow.",
      "",
    ].join("\n");
  }

  function statusClass(status) {
    return "hermes-runbooks-status hermes-runbooks-status-" + String(status || "draft");
  }

  function RunbooksApp() {
    const [overview, setOverview] = useState(null);
    const [selectedSlug, setSelectedSlug] = useState("");
    const [markdown, setMarkdown] = useState("");
    const [selected, setSelected] = useState(null);
    const [mode, setMode] = useState("edit");
    const [preview, setPreview] = useState("");
    const [diff, setDiff] = useState("");
    const [filter, setFilter] = useState("");
    const [view, setView] = useState("runbooks");
    const [proposalSummary, setProposalSummary] = useState("");
    const [legacyQuery, setLegacyQuery] = useState("");
    const [legacyResults, setLegacyResults] = useState([]);
    const [legacySummary, setLegacySummary] = useState(null);
    const [legacySelected, setLegacySelected] = useState(null);
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);

    const runbooks = overview ? overview.runbooks || [] : [];
    const workflows = overview ? overview.workflows || [] : [];
    const recentRuns = overview ? overview.recent_runs || [] : [];
    const schedules = overview ? overview.schedules || [] : [];

    function refresh() {
      setError("");
      return fetchJSON("/overview").then(setOverview).catch(function (err) {
        setError(err.message || String(err));
      });
    }

    useEffect(function () {
      refresh();
    }, []);

    useEffect(function () {
      if (!selectedSlug) return;
      setError("");
      fetchJSON("/runbooks/" + encodeURIComponent(selectedSlug)).then(function (data) {
        setSelected(data);
        setMarkdown(data.markdown || "");
        setMode("edit");
      }).catch(function (err) {
        setSelected(null);
        setMarkdown(emptyMarkdown(selectedSlug));
        setError(err.message || String(err));
      });
    }, [selectedSlug]);

    const visibleRunbooks = useMemo(function () {
      const q = filter.trim().toLowerCase();
      if (!q) return runbooks;
      return runbooks.filter(function (item) {
        return [item.slug, item.title, item.purpose, item.owner_profile, item.status]
          .join(" ").toLowerCase().indexOf(q) !== -1;
      });
    }, [runbooks, filter]);

    function saveActive() {
      setBusy(true);
      setError("");
      fetchJSON("/runbooks/" + encodeURIComponent(selectedSlug), {
        method: "PUT",
        body: JSON.stringify({ markdown: markdown }),
      }).then(function () {
        return refresh();
      }).then(function () {
        return fetchJSON("/runbooks/" + encodeURIComponent(selectedSlug));
      }).then(function (data) {
        setSelected(data);
        setMarkdown(data.markdown || markdown);
      }).catch(function (err) {
        setError(err.message || String(err));
      }).finally(function () {
        setBusy(false);
      });
    }

    function propose() {
      setBusy(true);
      setError("");
      fetchJSON("/runbooks/" + encodeURIComponent(selectedSlug) + "/proposals", {
        method: "POST",
        body: JSON.stringify({
          markdown: markdown,
          summary: proposalSummary || null,
        }),
      }).then(function () {
        setProposalSummary("");
        return fetchJSON("/runbooks/" + encodeURIComponent(selectedSlug));
      }).then(function (data) {
        setSelected(data);
      }).catch(function (err) {
        setError(err.message || String(err));
      }).finally(function () {
        setBusy(false);
      });
    }

    function renderPreview() {
      setMode("preview");
      fetchJSON("/runbooks/preview", {
        method: "POST",
        body: JSON.stringify({ markdown: markdown }),
      }).then(function (data) {
        setPreview(data.html || "");
      }).catch(function (err) {
        setError(err.message || String(err));
      });
    }

    function renderDiff() {
      setMode("diff");
      fetchJSON("/runbooks/" + encodeURIComponent(selectedSlug) + "/diff", {
        method: "POST",
        body: JSON.stringify({ markdown: markdown }),
      }).then(function (data) {
        setDiff(data.diff || "");
      }).catch(function (err) {
        setError(err.message || String(err));
      });
    }

    function newRunbook() {
      const slug = window.prompt("Runbook slug");
      if (!slug) return;
      setSelectedSlug(slug.trim());
      setSelected(null);
      setMarkdown(emptyMarkdown(slug.trim()));
      setMode("edit");
    }

    function searchLegacy() {
      setBusy(true);
      setError("");
      const suffix = legacyQuery.trim() ? "?q=" + encodeURIComponent(legacyQuery.trim()) : "";
      fetchJSON("/legacy" + suffix).then(function (data) {
        setLegacyResults(data.results || []);
        setLegacySummary(data.summary || null);
      }).catch(function (err) {
        setError(err.message || String(err));
      }).finally(function () {
        setBusy(false);
      });
    }

    function openLegacy(item) {
      fetchJSON("/legacy/" + encodeURIComponent(item.entity_type) + "/" + encodeURIComponent(item.entity_id))
        .then(setLegacySelected)
        .catch(function (err) { setError(err.message || String(err)); });
    }

    const selectedWorkflow = workflows.find(function (workflow) {
      return selected && selected.metadata && workflow.id === selected.metadata.id;
    });

    function workflowLabel(run) {
      const workflow = workflows.find(function (item) { return item.id === run.workflow_id; });
      return workflow ? workflow.name : run.workflow_id;
    }

    function displayTime(value) {
      if (!value) return "-";
      const date = new Date(typeof value === "number" ? value * 1000 : value);
      return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
    }

    return h("div", { className: "hermes-runbooks" },
      h("div", { className: "hermes-runbooks-topbar" },
        h("div", { className: "hermes-runbooks-kpis" },
          h("div", { className: "hermes-runbooks-kpi" },
            h("strong", null, overview ? overview.counts.active_workflows : "-"),
            h("span", null, "Active workflows")
          ),
          h("div", { className: "hermes-runbooks-kpi" },
            h("strong", null, overview ? overview.counts.enabled_schedules : "-"),
            h("span", null, "Enabled schedules")
          ),
          h("div", { className: "hermes-runbooks-kpi" },
            h("strong", null, overview ? overview.counts.unregistered_schedules : "-"),
            h("span", null, "Needs attention")
          )
        ),
        h("div", { className: "hermes-runbooks-actions" },
          h(Button, { onClick: refresh, variant: "outline", disabled: busy }, "Refresh"),
          view === "runbooks" ? h(Button, { onClick: newRunbook }, "New workflow") : null
        )
      ),
      error ? h("div", { className: "hermes-runbooks-error" }, error) : null,
      h("div", { className: "hermes-runbooks-tabs" },
        h(Button, {
          variant: view === "runbooks" ? "default" : "outline",
          onClick: function () { setView("runbooks"); },
        }, "Workflows"),
        h(Button, {
          variant: view === "schedules" ? "default" : "outline",
          onClick: function () { setView("schedules"); },
        }, "Schedules"),
        h(Button, {
          variant: view === "runs" ? "default" : "outline",
          onClick: function () { setView("runs"); },
        }, "Runs"),
        h(Button, {
          variant: view === "legacy" ? "default" : "outline",
          onClick: function () { setView("legacy"); setTimeout(searchLegacy, 0); },
        }, "Archive")
      ),
      view === "runbooks" ? h("div", { className: "hermes-runbooks-grid" },
        h("aside", { className: "hermes-runbooks-sidebar" },
          h(Label, null, "Search"),
          h(Input, {
            value: filter,
            onChange: function (event) { setFilter(event.target.value); },
            placeholder: "Filter workflows",
          }),
          h("div", { className: "hermes-runbooks-list" },
            visibleRunbooks.map(function (item) {
              return h("button", {
                key: item.slug,
                className: "hermes-runbooks-list-item " + (selectedSlug === item.slug ? "is-active" : ""),
                onClick: function () { setSelectedSlug(item.slug); },
              },
                h("span", { className: "hermes-runbooks-list-title" }, item.title),
                h("span", { className: "hermes-runbooks-list-meta" }, item.owner_profile),
                item.purpose ? h("span", { className: "hermes-runbooks-list-purpose" }, item.purpose) : null,
                h("span", { className: statusClass(item.status) }, item.status)
              );
            })
          )
        ),
        h("main", { className: "hermes-runbooks-main" },
          selectedSlug ? h(Card, null,
            h(CardContent, { className: "hermes-runbooks-panel" },
              h("div", { className: "hermes-runbooks-editor-head" },
                h("div", null,
                  h("h2", null, selected && selected.metadata ? selected.metadata.title : selectedSlug),
                  selected && selected.metadata ? h("div", { className: "hermes-runbooks-muted" },
                    selected.metadata.owner_profile + " | " + selected.metadata.status
                  ) : null
                ),
                h("div", { className: "hermes-runbooks-actions" },
                  h(Button, {
                    variant: mode === "edit" ? "default" : "outline",
                    onClick: function () { setMode("edit"); },
                  }, "Edit"),
                  h(Button, { variant: "outline", onClick: renderPreview }, "Preview"),
                  h(Button, { variant: "outline", onClick: renderDiff }, "Diff")
                )
              ),
              mode === "edit" ? h("textarea", {
                className: "hermes-runbooks-textarea",
                value: markdown,
                onChange: function (event) { setMarkdown(event.target.value); },
                spellCheck: false,
              }) : null,
              mode === "preview" ? h("div", {
                className: "hermes-runbooks-preview",
                dangerouslySetInnerHTML: { __html: preview },
              }) : null,
              mode === "diff" ? h("pre", { className: "hermes-runbooks-diff" }, diff || "No changes") : null,
              h("div", { className: "hermes-runbooks-savebar" },
                h(Button, { onClick: saveActive, disabled: busy || !selectedSlug }, "Approve Save"),
                h(Input, {
                  value: proposalSummary,
                  onChange: function (event) { setProposalSummary(event.target.value); },
                  placeholder: "Proposal summary",
                }),
                h(Button, {
                  variant: "outline",
                  onClick: propose,
                  disabled: busy || !selectedSlug,
                }, "Propose")
              )
            )
          ) : h("div", { className: "hermes-runbooks-empty" }, "Choose a workflow."),
          selectedWorkflow ? h(Card, null,
            h(CardContent, { className: "hermes-runbooks-panel" },
              h("div", { className: "hermes-runbooks-editor-head" },
                h("div", null,
                  h("h2", null, "Workflow"),
                  h("div", { className: "hermes-runbooks-muted" },
                    selectedWorkflow.id + " | version " + selectedWorkflow.version
                  )
                )
              ),
              h("div", { className: "hermes-runbooks-steps" },
                (selectedWorkflow.steps || []).map(function (step) {
                  return h("div", { key: step.step_key, className: "hermes-runbooks-step" },
                    h(Badge, { variant: "secondary" }, step.step_key),
                    h("span", null, step.name),
                    h("span", { className: "hermes-runbooks-muted" },
                      step.executor_profile || selectedWorkflow.owner_profile
                    )
                  );
                })
              )
            )
          ) : null,
          selected ? h(Card, null,
            h(CardContent, { className: "hermes-runbooks-panel" },
              h("h2", null, "History"),
              h("div", { className: "hermes-runbooks-history" },
                h("div", null,
                  h("h3", null, "Revisions"),
                  (selected.revisions || []).map(function (item) {
                    return h("code", { key: item.path }, item.name);
                  })
                ),
                h("div", null,
                  h("h3", null, "Proposals"),
                  (selected.proposals || []).map(function (item) {
                    return h("code", { key: item.created_at }, item.created_at + " " + (item.summary || ""));
                  })
                )
              )
            )
          ) : null
        )
      ) : view === "schedules" ? h("section", { className: "hermes-runbooks-table-panel" },
        h("div", { className: "hermes-runbooks-summary" },
          h("strong", null, overview ? overview.counts.enabled_schedules : 0),
          h("span", null, " enabled"),
          h("strong", null, overview ? overview.counts.registered_schedules : 0),
          h("span", null, " registered"),
          h("strong", null, overview ? overview.counts.unregistered_schedules : 0),
          h("span", null, " unregistered")
        ),
        h("div", { className: "hermes-runbooks-table-wrap" },
          h("table", { className: "hermes-runbooks-table" },
            h("thead", null, h("tr", null,
              h("th", null, "Workflow"),
              h("th", null, "Owner"),
              h("th", null, "Schedule"),
              h("th", null, "State"),
              h("th", null, "Registry")
            )),
            h("tbody", null, schedules.map(function (item) {
              return h("tr", { key: item.profile + ":" + item.job_id },
                h("td", null,
                  h("strong", null, item.name),
                  h("code", null, item.job_id)
                ),
                h("td", null, item.profile),
                h("td", null, h("code", null, item.schedule || "manual")),
                h("td", null, item.enabled ? (item.last_status || item.state) : "disabled"),
                h("td", null,
                  h("span", { className: statusClass(item.registration_status) }, item.registration_status),
                  item.workflow_slug ? h("code", null, item.workflow_slug) : null
                )
              );
            }))
          )
        )
      ) : view === "runs" ? h("section", { className: "hermes-runbooks-table-panel" },
        h("div", { className: "hermes-runbooks-summary" },
          h("strong", null, recentRuns.length),
          h("span", null, " recent runs"),
          h("strong", null, recentRuns.filter(function (run) { return run.status === "failed"; }).length),
          h("span", null, " failed")
        ),
        h("div", { className: "hermes-runbooks-table-wrap" },
          h("table", { className: "hermes-runbooks-table" },
            h("thead", null, h("tr", null,
              h("th", null, "Workflow"),
              h("th", null, "Status"),
              h("th", null, "Trigger"),
              h("th", null, "Current step"),
              h("th", null, "Started")
            )),
            h("tbody", null, recentRuns.map(function (run) {
              return h("tr", { key: run.id },
                h("td", null, h("strong", null, workflowLabel(run)), h("code", null, run.id)),
                h("td", null, h("span", { className: statusClass(run.status) }, run.status)),
                h("td", null, run.trigger_kind || "-"),
                h("td", null, run.current_step_key || "Complete"),
                h("td", null, displayTime(run.started_at))
              );
            }))
          )
        )
      ) : h("section", { className: "hermes-runbooks-table-panel" },
        h("div", { className: "hermes-runbooks-legacy-head" },
          h("div", { className: "hermes-runbooks-summary" },
            h("strong", null, legacySummary ? (legacySummary.source_counts.issues || 0) : 0),
            h("span", null, " archived tasks"),
            h("strong", null, legacySummary ? (legacySummary.source_counts.projects || 0) : 0),
            h("span", null, " projects"),
            h("strong", null, legacySummary ? (legacySummary.source_counts.routines || 0) : 0),
            h("span", null, " routines")
          ),
          h("div", { className: "hermes-runbooks-legacy-search" },
            h(Input, {
              value: legacyQuery,
              onChange: function (event) { setLegacyQuery(event.target.value); },
              onKeyDown: function (event) { if (event.key === "Enter") searchLegacy(); },
              placeholder: "Search past work",
            }),
            h(Button, { onClick: searchLegacy, disabled: busy }, "Search")
          )
        ),
        h("div", { className: "hermes-runbooks-legacy-layout" },
          h("div", { className: "hermes-runbooks-table-wrap" },
            h("table", { className: "hermes-runbooks-table hermes-runbooks-legacy-table" },
              h("thead", null, h("tr", null,
                h("th", null, "Legacy item"),
                h("th", null, "Type"),
                h("th", null, "Status"),
                h("th", null, "Updated")
              )),
              h("tbody", null, legacyResults.map(function (item) {
                return h("tr", {
                  key: item.entity_type + ":" + item.entity_id,
                  className: "hermes-runbooks-clickable-row",
                  onClick: function () { openLegacy(item); },
                },
                  h("td", null, h("strong", null, item.title || item.legacy_identifier), h("code", null, item.legacy_identifier)),
                  h("td", null, item.entity_type),
                  h("td", null, item.status || "-"),
                  h("td", null, item.updated_at || "-")
                );
              }))
            )
          ),
          legacySelected ? h("pre", { className: "hermes-runbooks-legacy-detail" },
            JSON.stringify(legacySelected.entity, null, 2)
          ) : h("div", { className: "hermes-runbooks-empty" }, "Choose a past item.")
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("runbooks", RunbooksApp);
})();
