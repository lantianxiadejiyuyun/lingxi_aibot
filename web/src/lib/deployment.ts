import { api, session } from './api'

export interface DeploymentInfo {
  public_url: string | null
  napcat: {
    managed: boolean
    webui_url: string | null
    http_url: string | null
    callback_url: string
    internal_callback_url?: string | null
    configure_available?: boolean
  }
  feishu: { managed: boolean }
}

export async function getDeployment(): Promise<DeploymentInfo | null> {
  if (!session.user?.is_admin) return null
  try { return await api<DeploymentInfo>('/deployment') } catch { return null }
}

export function deploymentUrl(value: string | null | undefined, origin = window.location.origin): string {
  if (!value) return ''
  try {
    const url = new URL(value, origin)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''
  } catch { return '' }
}
