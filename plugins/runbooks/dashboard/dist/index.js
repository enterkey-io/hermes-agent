(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const { React } = SDK;
  const h = React.createElement;
  const { useEffect, useMemo, useState } = SDK.hooks;
  const {
    Badge,
    Button,
    Card,
    CardContent,
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
    Input,
    Label,
    Separator,
    TabsList,
    TabsTrigger,
  } = SDK.components;
  const API = "/api/plugins/runbooks";

  function fetchJSON(path, options) {
    return SDK.fetchJSON(API + path, Object.assign({
      headers: { "Content-Type": "application/json" },
    }, options || {}));
  }

  function emptyMarkdown(slug) {
    return [
      "---",
      "id: wf_" + slug.replace(/[^a-zA-Z0-9_]/g, "_"),
      "slug: " + slug,
      "title: New Workflow",
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
      "# New Workflow",
      "",
      "## Procedure",
      "",
      "1. Prepare the workflow.",
      "",
    ].join("\n");
  }

  function displayTime(value, emptyLabel) {
    if (!value) return emptyLabel || "Not scheduled";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function statusTone(status) {
    const value = String(status || "").toLowerCase();
    if (["active", "ok", "succeeded", "completed", "registered", "enabled"].indexOf(value) !== -1) return "success";
    if (["failed", "execution_error", "error"].indexOf(value) !== -1) return "destructive";
    if (["paused", "degraded", "unregistered", "unresolved"].indexOf(value) !== -1) return "warning";
    return "outline";
  }

  function usefulPurpose(value) {
    const purpose = String(value || "").trim();
    if (purpose.toLowerCase().indexOf("canonical registry record for existing hermes cron job") === 0) return "";
    return purpose;
  }

  function StatusBadge(props) {
    const value = props.value || "unknown";
    return h(Badge, { tone: statusTone(value) }, props.label || value);
  }

  function RunbooksApp() {
    const [overview, setOverview] = useState(null);
    const [view, setView] = useState("workflows");
    const [filter, setFilter] = useState("");
    const [departmentFilter, setDepartmentFilter] = useState("");
    const [ownerFilter, setOwnerFilter] = useState("");
    const [selectedSlug, setSelectedSlug] = useState("");
    const [selected, setSelected] = useState(null);
    const [markdown, setMarkdown] = useState("");
    const [editorOpen, setEditorOpen] = useState(false);
    const [mode, setMode] = useState("edit");
    const [preview, setPreview] = useState("");
    const [diff, setDiff] = useState("");
    const [proposalSummary, setProposalSummary] = useState("");
    const [createOpen, setCreateOpen] = useState(false);
    const [newSlug, setNewSlug] = useState("");
    const [newDraft, setNewDraft] = useState(false);
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

    useEffect(function () { refresh(); }, []);

    useEffect(function () {
      if (!selectedSlug || newDraft) return;
      setError("");
      fetchJSON("/runbooks/" + encodeURIComponent(selectedSlug)).then(function (data) {
        setSelected(data);
        setMarkdown(data.markdown || "");
        setMode("edit");
      }).catch(function (err) {
        setSelected(null);
        setError(err.message || String(err));
      });
    }, [selectedSlug, newDraft]);

    const visibleRunbooks = useMemo(function () {
      const q = filter.trim().toLowerCase();
      return runbooks.filter(function (item) {
        const workflow = workflowForRunbook(item);
        const matchesText = !q || [item.slug, item.title, item.purpose, item.owner_profile, item.status,
          workflow && workflow.department, workflow && workflow.function]
          .join(" ").toLowerCase().indexOf(q) !== -1;
        const matchesDepartment = !departmentFilter || (workflow && workflow.department === departmentFilter);
        const matchesOwner = !ownerFilter || item.owner_profile === ownerFilter;
        return matchesText && matchesDepartment && matchesOwner;
      });
    }, [runbooks, workflows, filter, departmentFilter, ownerFilter]);

    const workflowDepartments = useMemo(function () {
      return Array.from(new Set(workflows.map(function (item) { return item.department; }).filter(Boolean))).sort();
    }, [workflows]);

    const workflowOwners = useMemo(function () {
      return Array.from(new Set(workflows.map(function (item) { return item.owner_profile; }).filter(Boolean))).sort();
    }, [workflows]);

    const enabledTimeline = useMemo(function () {
      return schedules.filter(function (item) { return item.enabled; }).sort(function (a, b) {
        if (a.registration_status !== b.registration_status) return a.registration_status === "registered" ? 1 : -1;
        const aTime = a.next_run_at ? new Date(a.next_run_at).getTime() : Number.MAX_SAFE_INTEGER;
        const bTime = b.next_run_at ? new Date(b.next_run_at).getTime() : Number.MAX_SAFE_INTEGER;
        return aTime - bTime;
      });
    }, [schedules]);

    function workflowForRunbook(item) {
      return workflows.find(function (entry) { return entry.id === item.id || entry.slug === item.slug; });
    }

    function workflowForSchedule(schedule) {
      return workflows.find(function (workflow) {
        return workflow.id === schedule.workflow_id || workflow.slug === schedule.workflow_slug;
      });
    }

    function workflowForRun(run) {
      return workflows.find(function (workflow) { return workflow.id === run.workflow_id; });
    }

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

    function openWorkflow(slug) {
      setNewDraft(false);
      setEditorOpen(false);
      setSelected(null);
      setSelectedSlug(slug);
    }

    function closeWorkflow() {
      setSelectedSlug("");
      setSelected(null);
      setEditorOpen(false);
      setNewDraft(false);
      setMode("edit");
    }

    function openCron(schedule) {
      const query = new URLSearchParams({
        profile: schedule.profile || "default",
        job: schedule.job_id,
      });
      window.location.assign("/cron?" + query.toString());
    }

    function controlWorkflow(workflow, action) {
      if (!workflow) return;
      const verb = action === "pause" ? "Pause" : (action === "start" ? "Start" : "Resume");
      if (!window.confirm(verb + " " + workflow.name + "? This changes the workflow state, updates its linked schedules, and creates an audit event.")) return;
      setBusy(true);
      setError("");
      fetchJSON("/workflows/" + encodeURIComponent(workflow.id) + "/control", {
        method: "POST",
        body: JSON.stringify({ action: action, expected_version: workflow.version, confirmed: true }),
      }).then(refresh).catch(function (err) {
        setError(err.message || String(err));
      }).finally(function () { setBusy(false); });
    }

    function beginNewWorkflow() {
      const slug = newSlug.trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
      if (!slug) return;
      setCreateOpen(false);
      setNewSlug("");
      setNewDraft(true);
      setSelected(null);
      setSelectedSlug(slug);
      setMarkdown(emptyMarkdown(slug));
      setEditorOpen(true);
      setMode("edit");
    }

    function saveActive() {
      setBusy(true);
      setError("");
      fetchJSON("/runbooks/" + encodeURIComponent(selectedSlug), {
        method: "PUT",
        body: JSON.stringify({ markdown: markdown }),
      }).then(refresh).then(function () {
        setNewDraft(false);
        return fetchJSON("/runbooks/" + encodeURIComponent(selectedSlug));
      }).then(function (data) {
        setSelected(data);
        setMarkdown(data.markdown || markdown);
        setEditorOpen(false);
      }).catch(function (err) {
        setError(err.message || String(err));
      }).finally(function () { setBusy(false); });
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
      }).finally(function () { setBusy(false); });
    }

    function renderPreview() {
      setMode("preview");
      fetchJSON("/runbooks/preview", {
        method: "POST",
        body: JSON.stringify({ markdown: markdown }),
      }).then(function (data) { setPreview(data.html || ""); })
        .catch(function (err) { setError(err.message || String(err)); });
    }

    function renderDiff() {
      setMode("diff");
      fetchJSON("/runbooks/" + encodeURIComponent(selectedSlug) + "/diff", {
        method: "POST",
        body: JSON.stringify({ markdown: markdown }),
      }).then(function (data) { setDiff(data.diff || ""); })
        .catch(function (err) { setError(err.message || String(err)); });
    }

    function searchLegacy() {
      setBusy(true);
      setError("");
      const suffix = legacyQuery.trim() ? "?q=" + encodeURIComponent(legacyQuery.trim()) : "";
      fetchJSON("/legacy" + suffix).then(function (data) {
        setLegacyResults(data.results || []);
        setLegacySummary(data.summary || null);
      }).catch(function (err) { setError(err.message || String(err)); })
        .finally(function () { setBusy(false); });
    }

    function openLegacy(item) {
      fetchJSON("/legacy/" + encodeURIComponent(item.entity_type) + "/" + encodeURIComponent(item.entity_id))
        .then(setLegacySelected)
        .catch(function (err) { setError(err.message || String(err)); });
    }

    function renderEditor() {
      return h("div", { className: "hermes-runbooks-editor" },
        h(TabsList, { className: "hermes-runbooks-mode-tabs" },
          h(TabsTrigger, { active: mode === "edit", value: "edit", onClick: function () { setMode("edit"); } }, "Edit"),
          h(TabsTrigger, { active: mode === "preview", value: "preview", onClick: renderPreview }, "Preview"),
          h(TabsTrigger, { active: mode === "diff", value: "diff", onClick: renderDiff }, "Changes")
        ),
        mode === "edit" ? h("textarea", {
          className: "hermes-runbooks-textarea",
          value: markdown,
          onChange: function (event) { setMarkdown(event.target.value); },
          spellCheck: false,
          "aria-label": "Workflow definition",
        }) : null,
        mode === "preview" ? h("div", {
          className: "hermes-runbooks-preview",
          dangerouslySetInnerHTML: { __html: preview },
        }) : null,
        mode === "diff" ? h("pre", { className: "hermes-runbooks-diff" }, diff || "No changes") : null,
        h("div", { className: "hermes-runbooks-proposal" },
          h(Input, {
            value: proposalSummary,
            onChange: function (event) { setProposalSummary(event.target.value); },
            placeholder: "Proposal summary (optional)",
          }),
          h(Button, { outlined: true, size: "sm", onClick: propose, disabled: busy || !selectedSlug }, "Propose")
        )
      );
    }

    function renderWorkflowDialog() {
      const item = runbooks.find(function (entry) { return entry.slug === selectedSlug; });
      const workflow = item ? workflowForRunbook(item) : null;
      const attached = schedulesFor(workflow);
      const steps = workflow ? workflow.steps || [] : [];
      const workflowRuns = runsFor(workflow);
      const latestRun = workflowRuns[0];
      const title = item ? item.title : "New Workflow";
      const purpose = item ? usefulPurpose(item.purpose) : "";

      return h(Dialog, {
        open: Boolean(selectedSlug),
        onOpenChange: function (open) { if (!open) closeWorkflow(); },
      },
        h(DialogContent, { className: "hermes-runbooks-dialog" },
          h(DialogHeader, null,
            h("div", { className: "hermes-runbooks-dialog-title" },
              h(DialogTitle, null, title),
              item ? h(StatusBadge, { value: item.canonical === false ? "proposed" : item.status }) : h(StatusBadge, { value: "draft" })
            ),
            h(DialogDescription, null,
              item ? (item.canonical === false ? "Pending proposal by " + item.owner_profile : (purpose || "Owned by " + item.owner_profile)) : "Create the workflow definition, then save it to the registry."
            )
          ),
          h("div", { className: "hermes-runbooks-dialog-body" },
            editorOpen ? renderEditor() : h(React.Fragment, null,
              h("section", { className: "hermes-runbooks-detail-section" },
                h("div", { className: "hermes-runbooks-section-heading" },
                  h("h3", null, "Schedule"),
                  h("span", null, attached.length ? attached.length + " attached" : "Manual")
                ),
                attached.length ? attached.map(function (schedule) {
                  return h("div", { key: schedule.profile + ":" + schedule.job_id, className: "hermes-runbooks-detail-row" },
                    h("div", { className: "hermes-runbooks-detail-copy" },
                      h("strong", null, schedule.name),
                      h("span", null, "Next run " + displayTime(schedule.next_run_at))
                    ),
                    h(StatusBadge, { value: schedule.last_status || schedule.state }),
                    h(Button, { outlined: true, size: "sm", onClick: function () { openCron(schedule); } }, "Edit in Cron")
                  );
                }) : h("p", { className: "hermes-runbooks-empty-copy" }, "This workflow runs only when started manually."),
                workflow && workflow.runtime_ref ? h("details", { className: "hermes-runbooks-advanced" },
                  h("summary", null, "Runtime details"),
                  h("code", null, workflow.runtime_ref)
                ) : null
              ),
              h(Separator, null),
              h("section", { className: "hermes-runbooks-detail-section" },
                h("div", { className: "hermes-runbooks-section-heading" },
                  h("h3", null, "Procedure"),
                  h("span", null, steps.length ? steps.length + (steps.length === 1 ? " step" : " steps") : "No steps")
                ),
                steps.length ? h("ol", { className: "hermes-runbooks-procedure" }, steps.map(function (step, index) {
                  return h("li", { key: step.step_key },
                    h("span", { className: "hermes-runbooks-step-number" }, String(index + 1)),
                    h("div", null,
                      h("strong", null, step.name),
                      h("span", null, step.executor_profile || workflow.owner_profile)
                    )
                  );
                })) : h("p", { className: "hermes-runbooks-empty-copy" }, "No procedure steps are registered."),
                h(Button, { ghost: true, size: "sm", onClick: function () { setEditorOpen(true); setMode("edit"); } }, "Edit procedure")
              ),
              h(Separator, null),
              h("section", { className: "hermes-runbooks-detail-section" },
                h("div", { className: "hermes-runbooks-section-heading" },
                  h("h3", null, "Latest activity"),
                  workflowRuns.length > 1 ? h("button", { className: "hermes-runbooks-text-link", onClick: function () { closeWorkflow(); setView("runs"); } }, "View all runs") : null
                ),
                latestRun ? h("div", { className: "hermes-runbooks-activity" },
                  h(StatusBadge, { value: latestRun.status }),
                  h("span", null, displayTime(latestRun.started_at)),
                  latestRun.current_step_key ? h("span", null, latestRun.current_step_key) : null
                ) : h("p", { className: "hermes-runbooks-empty-copy" }, "No runs have been recorded."),
                workflow ? h("details", { className: "hermes-runbooks-advanced" },
                  h("summary", null, "Advanced"),
                  h("dl", null,
                    h("div", null, h("dt", null, "Workflow ID"), h("dd", null, h("code", null, workflow.id))),
                    h("div", null, h("dt", null, "Owner"), h("dd", null, workflow.owner_profile)),
                    h("div", null, h("dt", null, "Version"), h("dd", null, workflow.version)),
                    h("div", null, h("dt", null, "Revisions"), h("dd", null, selected ? (selected.revisions || []).length : 0)),
                    h("div", null, h("dt", null, "Proposals"), h("dd", null, selected ? (selected.proposals || []).length : 0))
                  )
                ) : null
              )
            )
          ),
          h(DialogFooter, null,
            editorOpen ? h(React.Fragment, null,
              h(Button, { ghost: true, size: "sm", onClick: function () { if (newDraft) closeWorkflow(); else setEditorOpen(false); } }, "Cancel"),
              h(Button, { size: "sm", onClick: saveActive, disabled: busy || !selectedSlug }, busy ? "Saving" : "Save definition")
            ) : h(Button, { outlined: true, size: "sm", onClick: function () { setEditorOpen(true); setMode("edit"); } }, "Edit definition")
          )
        )
      );
    }

    function renderCreateDialog() {
      return h(Dialog, { open: createOpen, onOpenChange: setCreateOpen },
        h(DialogContent, { className: "hermes-runbooks-create-dialog" },
          h(DialogHeader, null,
            h(DialogTitle, null, "New Workflow"),
            h(DialogDescription, null, "Choose a stable identifier. You can set the title, owner, procedure, and runtime next.")
          ),
          h("div", { className: "hermes-runbooks-create-body" },
            h(Label, { htmlFor: "new-workflow-slug" }, "Workflow ID"),
            h(Input, {
              id: "new-workflow-slug",
              value: newSlug,
              onChange: function (event) { setNewSlug(event.target.value); },
              onKeyDown: function (event) { if (event.key === "Enter") beginNewWorkflow(); },
              placeholder: "daily-client-follow-up",
            })
          ),
          h(DialogFooter, null,
            h(Button, { ghost: true, size: "sm", onClick: function () { setCreateOpen(false); } }, "Cancel"),
            h(Button, { size: "sm", onClick: beginNewWorkflow, disabled: !newSlug.trim() }, "Continue")
          )
        )
      );
    }

    function renderWorkflows() {
      return h("section", { className: "hermes-runbooks-view" },
        h("div", { className: "hermes-runbooks-filter-grid" },
          h("div", { className: "hermes-runbooks-filter" },
            h(Label, { htmlFor: "workflow-search" }, "Search workflows"),
            h(Input, {
              id: "workflow-search",
              value: filter,
              onChange: function (event) { setFilter(event.target.value); },
              placeholder: "Name, purpose, owner, department, or status",
            })
          ),
          h("div", { className: "hermes-runbooks-filter" },
            h(Label, { htmlFor: "workflow-department" }, "Department"),
            h("select", {
              id: "workflow-department", value: departmentFilter,
              onChange: function (event) { setDepartmentFilter(event.target.value); },
            }, h("option", { value: "" }, "All departments"), workflowDepartments.map(function (name) {
              return h("option", { key: name, value: name }, name);
            }))
          ),
          h("div", { className: "hermes-runbooks-filter" },
            h(Label, { htmlFor: "workflow-owner" }, "Responsible agent"),
            h("select", {
              id: "workflow-owner", value: ownerFilter,
              onChange: function (event) { setOwnerFilter(event.target.value); },
            }, h("option", { value: "" }, "All agents"), workflowOwners.map(function (name) {
              return h("option", { key: name, value: name }, name);
            }))
          )
        ),
        h("div", { className: "hermes-runbooks-card-list" }, visibleRunbooks.map(function (item) {
          const workflow = workflowForRunbook(item);
          const purpose = usefulPurpose(item.purpose);
          const attached = schedulesFor(workflow);
          const latestRun = runsFor(workflow)[0];
          const nextSchedule = attached.filter(function (entry) { return entry.enabled && entry.next_run_at; })
            .sort(function (a, b) { return new Date(a.next_run_at) - new Date(b.next_run_at); })[0];
          return h(Card, { key: item.slug, className: "hermes-runbooks-workflow-card" },
            h(CardContent, { className: "hermes-runbooks-workflow-content" },
              h("div", { className: "hermes-runbooks-workflow-copy" },
                h("div", { className: "hermes-runbooks-workflow-title" },
                  h("h3", null, item.title),
                  h(StatusBadge, { value: item.status }),
                  h(Badge, { tone: "outline" }, item.owner_profile),
                  workflow && workflow.department ? h(Badge, { tone: "outline" }, workflow.department) : null
                ),
                purpose ? h("p", null, purpose) : null,
                h("div", { className: "hermes-runbooks-signals" },
                  h("span", null, attached.length ? attached.length + (attached.length === 1 ? " schedule" : " schedules") : "Manual"),
                  h("span", null, nextSchedule ? "Next " + displayTime(nextSchedule.next_run_at) : "No upcoming run"),
                  h("span", null, latestRun ? "Last run " + latestRun.status : "No runs yet")
                )
              ),
              h("div", { className: "hermes-runbooks-actions" },
                workflow && workflow.status === "active" ? h(Button, {
                  outlined: true, size: "sm", disabled: busy,
                  onClick: function () { controlWorkflow(workflow, "pause"); }
                }, "Pause") : null,
                workflow && workflow.status === "paused" ? h(Button, {
                  outlined: true, size: "sm", disabled: busy,
                  onClick: function () { controlWorkflow(workflow, "resume"); }
                }, "Resume") : null,
                workflow && workflow.status === "draft" ? h(Button, {
                  outlined: true, size: "sm", disabled: busy,
                  onClick: function () { controlWorkflow(workflow, "start"); }
                }, "Start") : null,
                h(Button, { outlined: true, size: "sm", onClick: function () { openWorkflow(item.slug); } }, "Open")
              )
            )
          );
        })),
        !visibleRunbooks.length ? h("p", { className: "hermes-runbooks-empty-copy" }, "No workflows match this search.") : null
      );
    }

    function renderTimeline() {
      return h("section", { className: "hermes-runbooks-view" },
        h("div", { className: "hermes-runbooks-view-heading" },
          h("p", null, enabledTimeline.length + " enabled schedules, ordered by next run"),
          h(Button, { outlined: true, size: "sm", onClick: function () { window.location.assign("/cron"); } }, "Open Cron")
        ),
        h("div", { className: "hermes-runbooks-card-list" }, enabledTimeline.map(function (schedule) {
          const workflow = workflowForSchedule(schedule);
          return h(Card, { key: schedule.profile + ":" + schedule.job_id, className: "hermes-runbooks-compact-card" },
            h(CardContent, { className: "hermes-runbooks-compact-content" },
              h("time", { className: "hermes-runbooks-next-time" }, displayTime(schedule.next_run_at)),
              h("div", { className: "hermes-runbooks-compact-copy" },
                h("div", { className: "hermes-runbooks-workflow-title" },
                  h("h3", null, workflow ? workflow.name : schedule.name),
                  h(StatusBadge, { value: schedule.registration_status })
                ),
                h("span", null, schedule.profile + " | " + (schedule.schedule || "manual") + (schedule.last_status ? " | last " + schedule.last_status : ""))
              ),
              workflow ? h(Button, { ghost: true, size: "sm", onClick: function () { openWorkflow(workflow.slug); } }, "Workflow") : null,
              h(Button, { outlined: true, size: "sm", onClick: function () { openCron(schedule); } }, "Edit in Cron")
            )
          );
        }))
      );
    }

    function renderRuns() {
      return h("section", { className: "hermes-runbooks-view" },
        h("div", { className: "hermes-runbooks-view-heading" },
          h("p", null, recentRuns.length + " recent workflow runs")
        ),
        h("div", { className: "hermes-runbooks-card-list" }, recentRuns.map(function (run) {
          const workflow = workflowForRun(run);
          return h(Card, { key: run.id, className: "hermes-runbooks-compact-card" },
            h(CardContent, { className: "hermes-runbooks-compact-content" },
              h(StatusBadge, { value: run.status }),
              h("div", { className: "hermes-runbooks-compact-copy" },
                h("h3", null, workflow ? workflow.name : run.workflow_id),
                h("span", null, displayTime(run.started_at) + " | " + (run.trigger_kind || "manual") + " | " + (run.current_step_key || "complete"))
              ),
              workflow ? h(Button, { outlined: true, size: "sm", onClick: function () { openWorkflow(workflow.slug); } }, "Open workflow") : null
            )
          );
        }))
      );
    }

    function renderArchive() {
      return h("section", { className: "hermes-runbooks-view" },
        h("div", { className: "hermes-runbooks-archive-tools" },
          h("p", null, legacySummary ? (legacySummary.source_counts.issues || 0) + " archived tasks, " + (legacySummary.source_counts.projects || 0) + " projects" : "Search read-only Paperclip history"),
          h("div", { className: "hermes-runbooks-archive-search" },
            h(Input, {
              value: legacyQuery,
              onChange: function (event) { setLegacyQuery(event.target.value); },
              onKeyDown: function (event) { if (event.key === "Enter") searchLegacy(); },
              placeholder: "Search past work",
            }),
            h(Button, { outlined: true, size: "sm", onClick: searchLegacy, disabled: busy }, "Search")
          )
        ),
        h("div", { className: "hermes-runbooks-archive-layout" },
          h("div", { className: "hermes-runbooks-card-list" }, legacyResults.map(function (item) {
            return h(Card, { key: item.entity_type + ":" + item.entity_id, className: "hermes-runbooks-compact-card" },
              h(CardContent, { className: "hermes-runbooks-compact-content" },
                h("div", { className: "hermes-runbooks-compact-copy" },
                  h("h3", null, item.title || item.legacy_identifier),
                  h("span", null, item.entity_type + " | " + (item.status || "unknown") + " | " + (item.updated_at || "unknown"))
                ),
                h(Button, { outlined: true, size: "sm", onClick: function () { openLegacy(item); } }, "Inspect")
              )
            );
          })),
          legacySelected ? h("pre", { className: "hermes-runbooks-legacy-detail" }, JSON.stringify(legacySelected.entity, null, 2)) : null
        )
      );
    }

    return h("div", { className: "hermes-runbooks" },
      h("div", { className: "hermes-runbooks-toolbar" },
        h("p", { className: "hermes-runbooks-overview" },
          h("strong", null, overview ? overview.counts.active_workflows : "-"), " active workflows",
          h("span", null, "|"),
          h("strong", null, overview ? overview.counts.enabled_schedules : "-"), " scheduled",
          h("span", null, "|"),
          h("strong", null, overview ? overview.counts.unregistered_schedules : "-"), " need attention"
        ),
        h("div", { className: "hermes-runbooks-actions" },
          h(Button, { ghost: true, size: "sm", onClick: refresh, disabled: busy }, "Refresh"),
          view === "workflows" ? h(Button, { size: "sm", onClick: function () { setCreateOpen(true); } }, "New workflow") : null
        )
      ),
      h(TabsList, { className: "hermes-runbooks-tabs" },
        h(TabsTrigger, { active: view === "workflows", value: "workflows", onClick: function () { setView("workflows"); } }, "Workflows"),
        h(TabsTrigger, { active: view === "timeline", value: "timeline", onClick: function () { setView("timeline"); } }, "Timeline"),
        h(TabsTrigger, { active: view === "runs", value: "runs", onClick: function () { setView("runs"); } }, "Runs"),
        h(TabsTrigger, { active: view === "legacy", value: "legacy", onClick: function () { setView("legacy"); setTimeout(searchLegacy, 0); } }, "Archive")
      ),
      error ? h("div", { className: "hermes-runbooks-error", role: "alert" }, error) : null,
      view === "workflows" ? renderWorkflows() : view === "timeline" ? renderTimeline() : view === "runs" ? renderRuns() : renderArchive(),
      renderWorkflowDialog(),
      renderCreateDialog()
    );
  }

  window.__HERMES_PLUGINS__.register("runbooks", RunbooksApp);
})();
