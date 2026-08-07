(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

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
    const [approver, setApprover] = useState("dashboard");
    const [proposalSummary, setProposalSummary] = useState("");
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);

    const runbooks = overview ? overview.runbooks || [] : [];
    const workflows = overview ? overview.workflows || [] : [];
    const recentRuns = overview ? overview.recent_runs || [] : [];

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
        body: JSON.stringify({ markdown: markdown, approved_by: approver }),
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
          proposed_by: approver,
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

    function startRun(workflow) {
      setBusy(true);
      fetchJSON("/runs", {
        method: "POST",
        body: JSON.stringify({
          workflow_id: workflow.id,
          trigger_kind: "manual",
          trigger_ref: "dashboard",
        }),
      }).then(refresh).catch(function (err) {
        setError(err.message || String(err));
      }).finally(function () {
        setBusy(false);
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

    const selectedWorkflow = workflows.find(function (workflow) {
      return selected && selected.metadata && workflow.id === selected.metadata.id;
    });

    return h("div", { className: "hermes-runbooks" },
      h("div", { className: "hermes-runbooks-topbar" },
        h("div", null,
          h("h1", null, "Runbooks"),
          h("div", { className: "hermes-runbooks-muted" },
            overview ? [
              overview.counts.runbooks + " runbooks",
              overview.counts.workflows + " workflows",
              overview.counts.recent_runs + " recent runs",
            ].join(" | ") : "Loading registry"
          )
        ),
        h("div", { className: "hermes-runbooks-actions" },
          h(Button, { onClick: refresh, variant: "outline", disabled: busy }, "Refresh"),
          h(Button, { onClick: newRunbook }, "New")
        )
      ),
      error ? h("div", { className: "hermes-runbooks-error" }, error) : null,
      h("div", { className: "hermes-runbooks-grid" },
        h("aside", { className: "hermes-runbooks-sidebar" },
          h(Label, null, "Search"),
          h(Input, {
            value: filter,
            onChange: function (event) { setFilter(event.target.value); },
            placeholder: "Filter runbooks",
          }),
          h("div", { className: "hermes-runbooks-list" },
            visibleRunbooks.map(function (item) {
              return h("button", {
                key: item.slug,
                className: "hermes-runbooks-list-item " + (selectedSlug === item.slug ? "is-active" : ""),
                onClick: function () { setSelectedSlug(item.slug); },
              },
                h("span", { className: "hermes-runbooks-list-title" }, item.title),
                h("span", { className: "hermes-runbooks-list-meta" },
                  item.slug + " | " + item.owner_profile
                ),
                h("span", { className: statusClass(item.status) }, item.status)
              );
            })
          ),
          h("section", { className: "hermes-runbooks-runs" },
            h("h2", null, "Recent Runs"),
            recentRuns.slice(0, 8).map(function (run) {
              return h("div", { key: run.id, className: "hermes-runbooks-run" },
                h("span", null, run.status),
                h("code", null, run.id)
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
                h(Label, null, "Approver"),
                h(Input, {
                  value: approver,
                  onChange: function (event) { setApprover(event.target.value); },
                }),
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
          ) : h("div", { className: "hermes-runbooks-empty" }, "Select or create a runbook."),
          selectedWorkflow ? h(Card, null,
            h(CardContent, { className: "hermes-runbooks-panel" },
              h("div", { className: "hermes-runbooks-editor-head" },
                h("div", null,
                  h("h2", null, "Workflow"),
                  h("div", { className: "hermes-runbooks-muted" },
                    selectedWorkflow.id + " | version " + selectedWorkflow.version
                  )
                ),
                h(Button, { onClick: function () { startRun(selectedWorkflow); }, disabled: busy },
                  "Start Run"
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
      )
    );
  }

  SDK.registerPlugin("runbooks", RunbooksApp);
})();
