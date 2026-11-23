export interface TrackingRuntimeConfig {
  enabled: boolean
  environment: string
}

export function loadMonitorConfig(): TrackingRuntimeConfig {
  return {
    enabled: !(window as unknown as Record<string, unknown>).__FARNA_ERROR_TRACKING_DISABLED__,
    environment: import.meta.env.MODE,
  }
}
