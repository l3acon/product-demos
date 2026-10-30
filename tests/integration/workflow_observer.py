"""Resolve Controller-provided child URLs and wait for complete workflow trees."""

import re
from urllib.parse import urlparse

from controller import ControllerError


TERMINAL = {"successful", "failed", "error", "canceled"}
COLLECTIONS = {
    "job": "jobs",
    "workflow_job": "workflow_jobs",
    "inventory_update": "inventory_updates",
    "project_update": "project_updates",
    "workflow_approval": "workflow_approvals",
}


def ready(job):
    return (
        job.get("status") in TERMINAL
        and job.get("event_processing_finished") is not False
    )


class WorkflowObserver:
    def __init__(self, runner):
        self.r = runner
        self.jobs = {}
        self.roots = {}
        self.nodes = {}
        self.parents = {}
        self.unresolved = set()
        self.stability = {}

    def remember(self, job):
        self.jobs[job["url"]] = job
        if job.get("type") == "workflow_job" and job["url"] not in self.parents:
            self.roots[job["url"]] = job

    def child_path(self, node):
        identifier = node["job"]
        if type(identifier) is not int or identifier <= 0:
            raise ControllerError("Workflow node has an invalid child job identifier")
        summary = node.get("summary_fields", {}).get("job", {})
        path = node.get("related", {}).get("job") or summary.get("url")
        if not path and summary.get("type") in COLLECTIONS:
            path = f"/api/controller/v2/{COLLECTIONS[summary['type']]}/{identifier}/"
        if not path:
            # Unified jobs has a LIST endpoint, not a per-ID detail endpoint.
            rows = self.r.api.list(
                f"/api/controller/v2/unified_jobs/?id={identifier}&page_size=1"
            )
            if len(rows) != 1 or rows[0].get("id") != identifier:
                raise ControllerError(
                    "Workflow child could not be resolved from Controller"
                )
            path = rows[0].get("url")
        parsed = urlparse(str(path or ""))
        if parsed.netloc:
            if (
                parsed.scheme != "https"
                or parsed.netloc != urlparse(getattr(self.r.api, "base", "")).netloc
            ):
                raise ControllerError("Workflow child URL changed API origin")
        match = re.fullmatch(
            r"/api/(?:controller/)?v2/(jobs|workflow_jobs|inventory_updates|project_updates|workflow_approvals)/(\d+)/",
            parsed.path,
        )
        if not match or int(match[2]) != identifier or parsed.query or parsed.fragment:
            raise ControllerError("Workflow child URL has an unexpected resource or ID")
        return f"/api/controller/v2/{match[1]}/{identifier}/"

    def inspect(self, workflow, ancestors=()):
        path = workflow["url"]
        if path in ancestors:
            raise ControllerError("Controller workflow ancestry contains a cycle")
        self.remember(workflow)
        self.r.active.add(path)
        try:
            nodes = self.r.api.list(path + "workflow_nodes/?page_size=200")
            self.nodes[path] = nodes
            settled = ready(workflow)
            signature = []
            inspection_errors = []
            for node in nodes:
                signature.append(
                    (
                        str(node.get("identifier", "")),
                        node.get("job"),
                        node.get("do_not_run"),
                    )
                )
                if not node.get("job"):
                    # A node still awaiting the scheduler is not an untaken branch.
                    missing_template = (
                        "unified_job_template" in node
                        and node["unified_job_template"] is None
                    )
                    if node.get("do_not_run") is False and not missing_template:
                        settled = False
                    continue
                try:
                    child_path = self.child_path(node)
                    self.parents[child_path] = {
                        "workflow_id": workflow["id"],
                        "workflow_name": workflow.get("name", "Workflow"),
                        "node_identifier": str(node.get("identifier", "")),
                    }
                    self.r.active.add(child_path)
                    child = self.r.api.get(child_path)
                    self.remember(child)
                    if child.get("type") == "workflow_job":
                        child_ready = self.inspect(child, (*ancestors, path))
                    else:
                        child_ready = ready(child)
                        if child_ready:
                            self.r.active.discard(child_path)
                    settled = child_ready and settled
                except ControllerError:
                    inspection_errors.append(str(node["job"]))
                    settled = False
            if inspection_errors:
                raise ControllerError(
                    "Could not observe workflow child jobs "
                    + ", ".join(inspection_errors)
                    + "; their typed API lookups failed"
                )
            fingerprint = tuple(sorted(signature, key=lambda row: row[0]))
            previous, count = self.stability.get(path, (None, 0))
            count = (
                count + 1
                if settled and previous == fingerprint
                else 1
                if settled
                else 0
            )
            self.stability[path] = (fingerprint, count)
            self.unresolved.discard(path)
            if settled and count >= 2:
                self.r.active.discard(path)
                return True
            return False
        except ControllerError:
            self.unresolved.add(path)
            raise

    def pending(self):
        rows = []
        for path in sorted(self.r.active | self.unresolved):
            job = self.jobs.get(path, {})
            match = re.search(r"/(\d+)/$", path)
            rows.append(
                {
                    "job_id": job.get("id", int(match[1]) if match else None),
                    "name": job.get("name", "Controller execution"),
                    "kind": job.get(
                        "type",
                        next(
                            (
                                kind
                                for kind, collection in COLLECTIONS.items()
                                if "/" + collection + "/" in path
                            ),
                            "job_template",
                        ),
                    ),
                    "controller_status": job.get("status", "unverified"),
                    "events_pending": job.get("event_processing_finished") is False,
                    "observation_incomplete": path in self.unresolved,
                    **self.parents.get(path, {}),
                }
            )
        return rows
