import { computed } from 'vue'
import { useCurrentUser } from './useCurrentUser'

/** Org roles, lowest first (the backend's `ORG_ROLE_HIERARCHY`). */
const ORG_ROLE_RANK: Record<string, number> = {
  viewer: 0,
  runner: 1,
  operator: 2,
  admin: 3,
}

/**
 * The minimum org role for each permission the pipeline editor's write controls
 * need. A copy of the matching entries in the backend registry
 * (`backend/src/farna/auth/permissions.py`); a unit test reads that file and
 * fails if the two drift apart.
 */
export const PIPELINE_EDITOR_PERMISSION_ROLES = {
  // POST /api/v1/runs
  'run.trigger': 'runner',
  // PUT .../graph, snapshot rollback, convert-to-agent, revert-to-manual
  'pipeline.graph.update': 'operator',
  // PATCH, archive, unarchive, pause, resume, autonomy demote/promote
  'pipeline.update': 'operator',
  // DELETE /api/v1/pipelines/{pipeline_id}
  'pipeline.delete': 'operator',
  // save-as-composite
  'pipeline.create': 'operator',
  // POST /api/v1/parameter-schemas/{schema_id}/sets
  'parameter_schema.set.create': 'operator',
  // PUT /api/v1/agents/{agent_id}/bindings
  'model_backend.binding.manage': 'operator',
} as const

export type PipelineEditorPermission = keyof typeof PIPELINE_EDITOR_PERMISSION_ROLES

/**
 * What the signed-in org role may do in the pipeline editor (FAR-2855).
 *
 * This only decides which controls are offered; the backend enforces access.
 * It answers the org-role question alone. Some endpoints add checks the access
 * token cannot answer (team membership on a team-private pipeline, admin for
 * raising a spend limit), so a control that passes here can still be refused,
 * and the editor shows that refusal. An unknown or missing role gets nothing,
 * as on the backend.
 */
export function usePipelineEditorPermissions() {
  const { orgRole } = useCurrentUser()

  const allows = (permission: PipelineEditorPermission) =>
    computed(
      () =>
        (ORG_ROLE_RANK[orgRole.value ?? ''] ?? -1) >=
        ORG_ROLE_RANK[PIPELINE_EDITOR_PERMISSION_ROLES[permission]],
    )

  return {
    canRunPipeline: allows('run.trigger'),
    canEditGraph: allows('pipeline.graph.update'),
    canUpdatePipeline: allows('pipeline.update'),
    canDeletePipeline: allows('pipeline.delete'),
    canCreatePipeline: allows('pipeline.create'),
    canCreateParamSet: allows('parameter_schema.set.create'),
    canManageRunnerBindings: allows('model_backend.binding.manage'),
  }
}
