import re
import subprocess
import unittest
from pathlib import Path


TEMPLATE = Path(__file__).parent / "templates" / "analytics.html"


class DashboardTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = TEMPLATE.read_text(encoding="utf-8")
        cls.script = re.search(r"<script>\s*(const \$\s*=.*?)(?=</script>)", cls.html, re.S).group(1)

    def test_sidebar_has_six_views_and_tools(self):
        for view in ("overview", "cash", "spending", "bills", "planning", "connection"):
            self.assertIn(f'class="side-link" data-tab="{view}', self.html)
        self.assertIn('aria-controls="dashboard-sidebar"', self.html)
        self.assertIn('id="sync-side"', self.html)
        self.assertIn('Purchase check', self.html)

    def test_navigation_accessibility_and_history(self):
        self.assertIn("aria-expanded", self.html)
        self.assertIn("aria-current", self.html)
        self.assertIn("history.pushState", self.script)
        self.assertIn("hashchange", self.script)
        self.assertIn("e.key==='Escape'", self.script)
        self.assertIn("menu-backdrop", self.script)

    def test_link_modes_and_truthful_affordability(self):
        self.assertIn("/api/create_link_token'+(update?'?update=1':'')", self.script)
        self.assertIn("if(!update)await fetch('/api/exchange_public_token'", self.script)
        self.assertIn("Insufficient confidence to assess affordability", self.script)
        self.assertNotIn("Not affordable'}`", self.script)

    def test_sorting_cache_and_empty_states(self):
        self.assertIn(".slice().sort((a,b)", self.script)
        self.assertIn("aria-sort", self.html)
        self.assertIn("cache.clear()", self.script)
        self.assertIn("No data for this range.", self.script)

    def test_chart_selects_canvas_and_keeps_fallback_table(self):
        self.assertIn("id[0]==='#'?id:'#'+id", self.script)
        self.assertIn("new Chart(canvas", self.script)
        self.assertIn("if(!window.Chart)", self.script)
        self.assertIn("fallbackTable(rows)", self.script)

    def test_overview_has_no_bills_dependency_and_empty_month_labels(self):
        self.assertIn("No monthly data", self.script)
        self.assertIn("Promise.allSettled", self.script)

    def test_mobile_grid_children_can_shrink(self):
        self.assertIn(".grid>*{min-width:0}", self.html)
        self.assertIn("overflow-x:hidden", self.html)

    def test_overview_runtime_uses_single_settled_loader_before_activation(self):
        self.assertEqual(self.script.count("async function overview()"), 1)
        self.assertEqual(self.script.count("function chart("), 1)
        self.assertNotIn("overview=", self.script)
        self.assertLess(self.script.index("async function overview()"), self.script.index("activate(location.hash"))

    def test_radar_runs_after_overview_even_when_overview_data_fails(self):
        self.assertIn("await overview();await radar('#overview-radar')", self.script)
        self.assertIn("Promise.allSettled", self.script)

    def test_chart_failure_keeps_accessible_fallback(self):
        self.assertIn("try{charts[id]=new Chart", self.html)
        self.assertIn("canvas.style.display='none'", self.html)
        self.assertIn("data table shown", self.html)

    def test_mobile_layout_is_single_column_and_width_bounded(self):
        self.assertIn(".kpis{grid-template-columns:minmax(0,1fr)}", self.html)
        self.assertIn(".wrap,.topbar,.banner,.layout,.main-content,.panel{max-width:100%}", self.html)
        self.assertIn(".cwrap{height:auto;min-height:0", self.html)

    def test_empty_chart_fallback_is_safe(self):
        self.assertIn("if(!rows.length)return", self.script)
        self.assertIn("No data for this range.", self.script)

    def test_source_has_no_mojibake_and_keeps_ui_labels(self):
        for marker in ("Ã", "Â", "â", "ð", "�"):
            self.assertNotIn(marker, self.html)
        for label in ("&#x2630;", "&#x2026;", "&#x2014;", "&#x00b7;"):
            self.assertIn(label, self.html)

    def test_javascript_parses(self):
        result = subprocess.run(["node", "--check"], input=self.script.encode("utf-8"),
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sync_result_isolated_and_reports_all_counts(self):
        for marker in ("added ${added}", "modified ${modified}", "removed ${removed}",
                       "Up to date: added 0, modified 0, removed 0.",
                       "Refresh of ${active} had errors.", "if(!r.ok)throw Error"):
            self.assertIn(marker, self.script)
        self.assertIn("setBusy(active,true)", self.script)

    def test_retry_reset_precedes_load_and_restores_chart(self):
        self.assertIn("function preparePanel(t)", self.script)
        self.assertIn("p.querySelectorAll('.state.error')", self.script)
        self.assertIn("el.classList.remove('error');el.textContent=''", self.script)
        self.assertIn("p.querySelectorAll('canvas').forEach(canvas=>{canvas.style.display=''})", self.script)
        self.assertLess(self.script.index("preparePanel(t);setBusy(t,true)"), self.script.index("await loadTab(t)"))

    def test_sync_refresh_error_does_not_timestamp_success(self):
        self.assertIn("if(refreshFailed)syncMessage+=` Refresh of ${active} had errors.`", self.script)
        self.assertIn("feedback(syncMessage,!refreshFailed)", self.script)

    def test_visible_loading_busy_settled_feedback_and_timestamp(self):
        self.assertIn('id="dashboard-feedback"', self.html)
        self.assertIn('id="last-updated"', self.html)
        self.assertIn("feedback(`Loading ${t}…`)", self.script)
        self.assertIn("setBusy(t,true)", self.script)
        self.assertIn("panelHasError(t)", self.script)
        self.assertIn("toLocaleTimeString()", self.script)

    def test_no_automatic_polling(self):
        self.assertNotIn("setInterval", self.script)

    def test_budget_planner_uses_calendar_month_and_complete_states(self):
        for marker in ('type="month"', 'id="budget-calendar"', 'id="budget-agenda"',
                       'Monthly planned budget', 'Posted transactions and cached bills',
                       "/api/budget/month?month=", "if(t==='planning')await loadBudget()",
                       "No posted transactions or scheduled bills this month.",
                       "Budget data unavailable."):
            self.assertIn(marker, self.html)
        self.assertIn("new Date().toISOString().slice(0,7)", self.script)
        self.assertNotIn("Date.now()-30", self.script)

    def test_analyst_history_replaces_existing_render(self):
        self.assertIn("m.replaceChildren();ev.replaceChildren()", self.script)

    def test_analyst_view_contract_and_safety(self):
        for marker in ("Analyst", "/api/analyst/history", "/api/analyst/chat", "/api/analyst/events",
                       "Confirm and add to plan", "confirmed:true", "aria-live=\"polite\"",
                       "document.createTextNode(String(text??''))", "Kimi setup unavailable"):
            self.assertIn(marker, self.html)
        self.assertNotIn("innerHTML=text", self.script)

    def test_analyst_requires_confirmation_and_no_polling(self):
        self.assertIn("if(d.proposed_event)", self.script)
        self.assertIn("replaceChildren()", self.script)
        self.assertIn("analyst-form", self.html)


if __name__ == "__main__":
    unittest.main()
