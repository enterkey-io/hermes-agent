(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const { React } = SDK;
  const h = React.createElement;
  const { useEffect, useMemo, useState } = SDK.hooks;
  const { Badge, Button, Input, Label } = SDK.components;
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

  function displayTime(value) {
    if (!value) return "Not scheduled";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function RunbooksApp() {
    const [overview, setOverview] = useState(null);
    const [selectedSlug, setSelectedSlug] = useState("");
    const [markdown, setMarkdown] = useState("");
    const [selected, setSelected] = useState(null);
    const [editorOpen, setEditorOpen] = useState(false);
    const [mode, setMode] = useState("edit");
    const [preview, setPreview] = useState("");
    const [diff, setDiff] = useState("");
    const [filter, setFilter] = useState("");
    const [view, setView] = useState("workflows");
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
        setEditorOpen(true);
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

    const enabledTimeline = useMemo(function () {
      return schedules.filter(function (item) { return item.enabled; }).sort(function (a, b) {
        if (a.registration_status !== b.registration_status) {
          return a.registration_status === "registered" ? 1 : -1;
        }
        const aTime = a.next_run_at ? new Date(a.next_run_at).getTime() : Number.MAX_SAFE_INTEGER;
        const bTime = b.next_run_at ? new Date(b.next_run_at).getTime() : Number.MAX_SAFE_INTEGER;
        return aTime - bTime;
      });
    }, [schedules]);

    function schedulesFor(workflow) {
      if (!workflow) return [];
      return schedules.filter(function (item) {
        return item.workflow_id === workflow.id || item.workflow_slug === workflow.slug;
      });
    }

    function runsFor(workflow) {
      if (!workflow) return [];
      return recentRuns.filter(function (run) { return run.workflow_id === workflow.id; });
    }

    function workflowForSchedule(schedule) {
      return workflows.find(function (workflow) {
        return workflow.id === schedule.workflow_id || workflow.slug === schedule.workflow_slug;
      });
    }

    function workflowForRun(run) {
      return workflows.find(function (workflow) { return workflow.id === run.workflow_id; });
    }

    function openWorkflow(slug) {
      if (!slug) return;
      if (slug !== selectedSlug) setSelected(null);
      setSelectedSlug(slug);
      setEditorOpen(false);
      setView("workflows");
      setTimeout(function () {
        const detail = document.getElementById("workflow-" + slug);
        if (detail) detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }, 0);
    }

    function openCron(schedule) {
      const query = new URLSearchParams({
        profile: schedule.profile || "default",
        job: schedule.job_id,
      });
      window.location.assign("/cron?" + query.toString());
    }

    function saveActive() {
      setBusy(true);
      setError("");
      fetchJSON("/runbooks/" + encodeURIComponent(selectedSlug), {
        method: "PUT",
        body: JSON.stringify({ markdown: markdown }),
      }).then(refresh).then(function () {
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
        body: JSON.stringify({ markdown: markdown, summary: proposalSummary || null }),
      }).then(function () {
        setProposalSummary("");
        return fetchJSON("/runbooks/" + encodeURIComponent(selectedSlug));
      }).then(setSelected).catch(function (err) {
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
      const slug = window.prompt("Workflow slug");
      if (!slug) return;
      setSelectedSlug(slug.trim());
      setSelected(null);
      setMarkdown(emptyMarkdown(slug.trim()));
      setEditorOpen(true);
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

    function renderDefinitionEditor() {
      if (!editorOpen || !selectedSlug) return null;
      return h("section", { className: "hermes-runbooks-definition" },
        h("div", { className: "hermes-runbooks-editor-head" },
          h("h3", null, "Workflow definition"),
          h("div", { className: "hermes-runbooks-actions" },
            h(Button, {
              variant: mode === "edit" ? "default" : "outline",
              onClick: function () { setMode("edit"); },
            }, "Edit"),
            h(Button, { variant: "outline", onClick: renderPreview }, "Preview"),
            h(Button, { variant: "outline", onClick: renderDiff }, "Diff"),
            h(Button, { variant: "outline", onClick: function () { setEditorOpen(false); } }, "Close")
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
          h(Button, { onClick: saveActive, disabled: busy || !selectedSlug }, "Save definition"),
          h(Input, {
            value: proposalSummary,
            onChange: function (event) { setProposalSummary(event.target.value); },
            placeholder: "Proposal summary",
          }),
          h(Button, { variant: "outline", onClick: propose, disabled: busy || !selectedSlug }, "Propose")
        )
      );
    }

    function renderWorkflowDetail(item, workflow) {
      if (selectedSlug !== item.slug) return null;
      const attached = schedulesFor(workflow);
      const workflowRuns = runsFor(workflow).slice(0, 6);
      const steps = workflow ? workflow.steps || [] : [];
      return h("div", { className: "hermes-runbooks-workflow-detail" },
        h("div", { className: "hermes-runbooks-detail-head" },
          h("div", null,
            h("h2", null, item.title),
            h("div", { className: "hermes-runbooks-muted" },
              item.owner_profile + " | " + item.status + (workflow ? " | version " + workflow.version : "")
            )
          ),
          h("div", { className: "hermes-runbooks-actions" },
            h(Button, { variant: "outline", onClick: function () { setEditorOpen(!editorOpen); } },
              editorOpen ? "Hide definition" : "Edit definition"
            ),
            h(Button, { variant: "outline", onClick: function () { setSelectedSlug(""); setSelected(null); } }, "Close")
          )
        ),
        h("div", { className: "hermes-runbooks-relationship-grid" },
          h("section", { className: "hermes-runbooks-relationship" },
            h("div", { className: "hermes-runbooks-section-title" },
              h("h3", null, "Attached schedules"),
              h("span", null, String(attached.length))
            ),
            attached.length ? attached.map(function (schedule) {
              return h("div", { key: schedule.profile + ":" + schedule.job_id, className: "hermes-runbooks-schedule" },
                h("div", { className: "hermes-runbooks-schedule-main" },
                  h("strong", null, schedule.name),
                  h("code", null, schedule.schedule || "manual"),
                  h("span", { className: "hermes-runbooks-muted" }, "Next: " + displayTime(schedule.next_run_at))
                ),
                h("div", { className: "hermes-runbooks-schedule-actions" },
                  h("span", { className: statusClass(schedule.last_status || schedule.state) }, schedule.last_status || schedule.state),
                  h(Button, { variant: "outline", onClick: function () { openCron(schedule); } }, "Edit in Cron")
                )
              );
            }) : h("div", { className: "hermes-runbooks-empty-inline" }, "Manual workflow"),
            workflow && workflow.runtime_ref ? h("div", { className: "hermes-runbooks-runtime" },
              h("span", null, "Runtime"), h("code", null, workflow.runtime_ref)
            ) : null
          ),
          h("section", { className: "hermes-runbooks-relationship" },
            h("div", { className: "hermes-runbooks-section-title" },
              h("h3", null, "Steps"),
              h("span", null, String(steps.length))
            ),
            steps.length ? h("ol", { className: "hermes-runbooks-steps" }, steps.map(function (step) {
              return h("li", { key: step.step_key, className: "hermes-runbooks-step" },
                h(Badge, { variant: "secondary" }, step.step_key),
                h("span", null, step.name),
                h("span", { className: "hermes-runbooks-muted" }, step.executor_profile || workflow.owner_profile)
              );
            })) : h("div", { className: "hermes-runbooks-empty-inline" }, "No registered steps")
          ),
          h("section", { className: "hermes-runbooks-relationship" },
            h("div", { className: "hermes-runbooks-section-title" },
              h("h3", null, "Recent runs"),
              h("span", null, String(workflowRuns.length))
            ),
            workflowRuns.length ? workflowRuns.map(function (run) {
              return h("div", { key: run.id, className: "hermes-runbooks-run" },
                h("span", { className: statusClass(run.status) }, run.status),
                h("span", null, displayTime(run.started_at)),
                h("span", { className: "hermes-runbooks-muted" }, run.current_step_key || "Complete")
              );
            }) : h("div", { className: "hermes-runbooks-empty-inline" }, "No recorded runs")
          )
        ),
        renderDefinitionEditor(),
        selected ? h("details", { className: "hermes-runbooks-history" },
          h("summary", null, "Revision and proposal history"),
          h("div", { className: "hermes-runbooks-history-grid" },
            h("div", null,
              h("h3", null, "Revisions"),
              (selected.revisions || []).map(function (revision) {
                return h("code", { key: revision.path }, revision.name);
              })
            ),
            h("div", null,
              h("h3", null, "Proposals"),
              (selected.proposals || []).map(function (proposal) {
                return h("code", { key: proposal.created_at }, proposal.created_at + " " + (proposal.summary || ""));
              })
            )
          )
        ) : null
      );
    }

    function renderWorkflows() {
      return h("section", { className: "hermes-runbooks-workflows" },
        h("div", { className: "hermes-runbooks-filter" },
          h(Label, null, "Search workflows"),
          h(Input, {
            value: filter,
            onChange: function (event) { setFilter(event.target.value); },
            placeholder: "Name, owner, status",
          })
        ),
        h("div", { className: "hermes-runbooks-list" }, visibleRunbooks.map(function (item) {
          const workflow = workflows.find(function (entry) { return entry.id === item.id || entry.slug === item.slug; });
          const attached = schedulesFor(workflow);
          const workflowRuns = runsFor(workflow);
          const latestRun = workflowRuns[0];
          const nextSchedule = attached.filter(function (entry) { return entry.enabled && entry.next_run_at; })
            .sort(function (a, b) { return new Date(a.next_run_at) - new Date(b.next_run_at); })[0];
          return h(React.Fragment, { key: item.slug },
            h("button", {
              id: "workflow-" + item.slug,
              className: "hermes-runbooks-list-item " + (selectedSlug === item.slug ? "is-active" : ""),
              onClick: function () { openWorkflow(item.slug); },
            },
              h("span", { className: "hermes-runbooks-list-title" }, item.title),
              h("span", { className: "hermes-runbooks-list-owner" }, item.owner_profile),
              h("span", { className: statusClass(item.status) }, item.status),
              h("span", { className: "hermes-runbooks-connection" },
                attached.length + (attached.length === 1 ? " schedule" : " schedules")
              ),
              h("span", { className: "hermes-runbooks-connection" },
                nextSchedule ? "Next " + displayTime(nextSchedule.next_run_at) : "Manual or paused"
              ),
              h("span", { className: "hermes-runbooks-connection" },
                latestRun ? "Last run " + latestRun.status : "No runs"
              )
            ),
            renderWorkflowDetail(item, workflow)
          );
        }))
      );
    }

    function renderTimeline() {
      return h("section", { className: "hermes-runbooks-table-panel" },
        h("div", { className: "hermes-runbooks-view-head" },
          h("div", { className: "hermes-runbooks-summary" },
            h("strong", null, enabledTimeline.length), h("span", null, " upcoming schedules"),
            h("strong", null, overview ? overview.counts.unregistered_schedules : 0), h("span", null, " need registration")
          ),
          h(Button, { variant: "outline", onClick: function () { window.location.assign("/cron"); } }, "Open Cron manager")
        ),
        h("div", { className: "hermes-runbooks-table-wrap" },
          h("table", { className: "hermes-runbooks-table" },
            h("thead", null, h("tr", null,
              h("th", null, "Next run"),
              h("th", null, "Workflow"),
              h("th", null, "Timing"),
              h("th", null, "Last result"),
              h("th", null, "Schedule editor")
            )),
            h("tbody", null, enabledTimeline.map(function (schedule) {
              const workflow = workflowForSchedule(schedule);
              return h("tr", { key: schedule.profile + ":" + schedule.job_id },
                h("td", null, h("strong", null, displayTime(schedule.next_run_at))),
                h("td", null,
                  workflow ? h("button", { className: "hermes-runbooks-link", onClick: function () { openWorkflow(workflow.slug); } }, workflow.name) : h("strong", null, schedule.name),
                  h("span", { className: statusClass(schedule.registration_status) }, schedule.registration_status),
                  h("code", null, schedule.profile + " / " + schedule.job_id)
                ),
                h("td", null, h("code", null, schedule.schedule || "manual")),
                h("td", null,
                  h("span", { className: statusClass(schedule.last_status || schedule.state) }, schedule.last_status || schedule.state),
                  schedule.last_run_at ? h("code", null, displayTime(schedule.last_run_at)) : null
                ),
                h("td", null, h(Button, { variant: "outline", onClick: function () { openCron(schedule); } }, "Edit in Cron"))
              );
            }))
          )
        )
      );
    }

    function renderRuns() {
      return h("section", { className: "hermes-runbooks-table-panel" },
        h("div", { className: "hermes-runbooks-summary" },
          h("strong", null, recentRuns.length), h("span", null, " recent runs"),
          h("strong", null, recentRuns.filter(function (run) { return run.status === "failed"; }).length), h("span", null, " failed")
        ),
        h("div", { className: "hermes-runbooks-table-wrap" },
          h("table", { className: "hermes-runbooks-table" },
            h("thead", null, h("tr", null,
              h("th", null, "Workflow"), h("th", null, "Status"), h("th", null, "Trigger"), h("th", null, "Step"), h("th", null, "Started")
            )),
            h("tbody", null, recentRuns.map(function (run) {
              const workflow = workflowForRun(run);
              return h("tr", { key: run.id },
                h("td", null,
                  workflow ? h("button", { className: "hermes-runbooks-link", onClick: function () { openWorkflow(workflow.slug); } }, workflow.name) : h("strong", null, run.workflow_id),
                  h("code", null, run.id)
                ),
                h("td", null, h("span", { className: statusClass(run.status) }, run.status)),
                h("td", null, run.trigger_kind || "-"),
                h("td", null, run.current_step_key || "Complete"),
                h("td", null, displayTime(run.started_at))
              );
            }))
          )
        )
      );
    }

    function renderArchive() {
      return h("section", { className: "hermes-runbooks-table-panel" },
        h("div", { className: "hermes-runbooks-legacy-head" },
          h("div", { className: "hermes-runbooks-summary" },
            h("strong", null, legacySummary ? (legacySummary.source_counts.issues || 0) : 0), h("span", null, " archived tasks"),
            h("strong", null, legacySummary ? (legacySummary.source_counts.projects || 0) : 0), h("span", null, " projects"),
            h("strong", null, legacySummary ? (legacySummary.source_counts.routines || 0) : 0), h("span", null, " routines")
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
              h("thead", null, h("tr", null, h("th", null, "Legacy item"), h("th", null, "Type"), h("th", null, "Status"), h("th", null, "Updated"))),
              h("tbody", null, legacyResults.map(function (item) {
                return h("tr", {
                  key: item.entity_type + ":" + item.entity_id,
                  className: "hermes-runbooks-clickable-row",
                  onClick: function () { openLegacy(item); },
                },
                  h("td", null, h("strong", null, item.title || item.legacy_identifier), h("code", null, item.legacy_identifier)),
                  h("td", null, item.entity_type), h("td", null, item.status || "-"), h("td", null, item.updated_at || "-")
                );
              }))
            )
          ),
          legacySelected ? h("pre", { className: "hermes-runbooks-legacy-detail" }, JSON.stringify(legacySelected.entity, null, 2))
            : h("div", { className: "hermes-runbooks-empty" }, "Choose a past item.")
        )
      );
    }

    return h("div", { className: "hermes-runbooks" },
      h("div", { className: "hermes-runbooks-topbar" },
        h("div", { className: "hermes-runbooks-kpis" },
          h("div", { className: "hermes-runbooks-kpi" }, h("strong", null, overview ? overview.counts.active_workflows : "-"), h("span", null, "Active workflows")),
          h("div", { className: "hermes-runbooks-kpi" }, h("strong", null, overview ? overview.counts.enabled_schedules : "-"), h("span", null, "Attached schedules")),
          h("div", { className: "hermes-runbooks-kpi" }, h("strong", null, overview ? overview.counts.unregistered_schedules : "-"), h("span", null, "Needs attention"))
        ),
        h("div", { className: "hermes-runbooks-actions" },
          h(Button, { onClick: refresh, variant: "outline", disabled: busy }, "Refresh"),
          view === "workflows" ? h(Button, { onClick: newRunbook }, "New workflow") : null
        )
      ),
      error ? h("div", { className: "hermes-runbooks-error" }, error) : null,
      h("div", { className: "hermes-runbooks-tabs" },
        h(Button, { variant: view === "workflows" ? "default" : "outline", onClick: function () { setView("workflows"); } }, "Workflows"),
        h(Button, { variant: view === "timeline" ? "default" : "outline", onClick: function () { setView("timeline"); } }, "Timeline"),
        h(Button, { variant: view === "runs" ? "default" : "outline", onClick: function () { setView("runs"); } }, "Runs"),
        h(Button, { variant: view === "legacy" ? "default" : "outline", onClick: function () { setView("legacy"); setTimeout(searchLegacy, 0); } }, "Archive")
      ),
      view === "workflows" ? renderWorkflows() : view === "timeline" ? renderTimeline() : view === "runs" ? renderRuns() : renderArchive()
    );
  }

  window.__HERMES_PLUGINS__.register("runbooks", RunbooksApp);
})();
