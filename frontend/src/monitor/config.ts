import { isErrorTrackingDisabled } from '../config/runtime'

export interface TrackingRuntimeConfig {
  enabled: boolean
  environment: string
}

export function loadMonitorConfig(): TrackingRuntimeConfig {
  return {
    enabled: !isErrorTrackingDisabled(),
    environment: import.meta.env.MODE,
  }
}
