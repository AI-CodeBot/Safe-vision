const API_BASE = (import.meta.env.VITE_API_URL || "").replace(/\/$/, "");

export async function api(path, options) {
  const response = await fetch(`${API_BASE}${path}`, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `SafeVision service returned ${response.status}.`);
  }
  return data;
}

export function apiUrl(path) {
  return `${API_BASE}${path}`;
}
