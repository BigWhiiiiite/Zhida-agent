import type { ResumeProfile, ResumeRecord } from './types'

const API = import.meta.env.VITE_API_URL ?? 'http://localhost:8000/api'

async function result<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail ?? '请求失败')
  }
  return response.json()
}

export const api = {
  list: () => fetch(`${API}/resumes`).then(result<ResumeRecord[]>),
  upload: (file: File) => {
    const data = new FormData(); data.append('file', file)
    return fetch(`${API}/resumes`, { method: 'POST', body: data }).then(result<ResumeRecord>)
  },
  save: (id: string, label: string, profile: ResumeProfile) =>
    fetch(`${API}/resumes/${id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label, profile })
    }).then(result<ResumeRecord>),
}

