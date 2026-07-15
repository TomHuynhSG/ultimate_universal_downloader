import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Clock,
  Download,
  FolderOpen,
  HardDrive,
  Image,
  Info,
  Layers,
  Link,
  Pause,
  Play,
  PlayCircle,
  RotateCw,
  Search,
  Server,
  Settings,
  Trash2,
  XCircle,
} from 'lucide-react'
import './index.css'


const API_ROOT = '/api'
const ACTIVE_STATUSES = new Set(['pending', 'extracting', 'downloading', 'paused'])
const FINISHED_STATUSES = new Set(['completed', 'completed_with_errors', 'error', 'failed'])

const formatBytes = (bytes) => {
  if (!bytes) return '0 B'
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), sizes.length - 1)
  return `${parseFloat((bytes / Math.pow(1024, index)).toFixed(2))} ${sizes[index]}`
}

const formatTime = (seconds) => {
  if (!Number.isFinite(seconds)) return '0s'
  if (seconds < 60) return `${Math.floor(seconds)}s`
  const minutes = Math.floor(seconds / 60)
  const remainder = Math.floor(seconds % 60)
  if (minutes < 60) return `${minutes}m ${remainder}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

const taskDetails = (value) => {
  if (!value) return {}
  if (typeof value === 'object') return value
  try {
    const parsed = JSON.parse(value)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

const apiFetch = async (path, options) => {
  const response = await fetch(`${API_ROOT}${path}`, options)
  if (!response.ok) {
    let message = `Request failed with HTTP ${response.status}`
    try {
      const payload = await response.json()
      message = payload.detail || message
    } catch {
      // The status text above is enough when the server did not return JSON.
    }
    throw new Error(message)
  }
  return response
}

const TaskCard = memo(function TaskCard({
  task,
  number,
  onPause,
  onResume,
  onRestart,
  onDelete,
  onOpen,
  onLogs,
  onDeleteChapter,
}) {
  const details = useMemo(() => taskDetails(task.details), [task.details])
  const chapters = useMemo(
    () => Object.entries(details).filter(([name]) => !name.startsWith('_')),
    [details],
  )
  const meta = details._meta || {}
  const totals = useMemo(
    () => chapters.reduce(
      (result, [, stats]) => ({
        total: result.total + (stats.total || 0),
        done: result.done + (stats.done || 0),
        failed: result.failed + (stats.failed || 0),
        size: result.size + (stats.size_bytes || 0),
      }),
      { total: 0, done: 0, failed: 0, size: 0 },
    ),
    [chapters],
  )
  const visibleChapters = chapters.slice(0, 60)

  let borderColor = 'rgba(255,255,255,0.05)'
  if (task.status === 'completed') borderColor = 'rgba(16,185,129,0.6)'
  if (task.status === 'completed_with_errors') borderColor = 'rgba(234,179,8,0.7)'
  if (task.status === 'extracting') borderColor = 'rgba(59,130,246,0.6)'
  if (['error', 'failed'].includes(task.status)) borderColor = 'rgba(239,68,68,0.6)'

  return (
    <article className="glass-panel" style={{ padding: '1.5rem', border: `3px solid ${borderColor}` }}>
      <div style={{ display: 'flex', gap: '16px', alignItems: 'center' }}>
        {task.thumbnail && (
          <img
            loading="lazy"
            src={`${API_ROOT}/proxy?url=${encodeURIComponent(task.thumbnail)}&referer=${encodeURIComponent(task.url)}`}
            alt="Thumbnail"
            style={{ flexShrink: 0, width: '96px', height: '128px', borderRadius: '8px', objectFit: 'cover' }}
          />
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          <h3 style={{ display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '4px' }}>
            <span style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>#{number}</span>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{task.title}</span>
          </h3>
          <a href={task.url} target="_blank" rel="noreferrer" style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
            {task.url}
          </a>

          {task.status === 'extracting' && meta.total > 0 && (
            <p style={{ color: '#60a5fa', marginTop: '8px', fontSize: '0.85rem' }}>
              Extracting gallery {meta.completed || 0} / {meta.total}
              {meta.errors ? ` • ${meta.errors} failed` : ''}
            </p>
          )}

          <div style={{ height: '6px', marginTop: '12px', background: 'rgba(255,255,255,0.1)', borderRadius: '99px', overflow: 'hidden' }}>
            <div style={{ width: `${task.progress}%`, height: '100%', background: 'var(--accent-primary)', transition: 'width .25s ease' }} />
          </div>

          {visibleChapters.length > 0 && (
            <div style={{ marginTop: '14px', maxHeight: '150px', overflowY: 'auto', display: 'grid', gap: '9px' }}>
              {visibleChapters.map(([chapter, stats], index) => (
                <div key={chapter} style={{ fontSize: '0.78rem' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px' }}>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      <span style={{ opacity: 0.5, marginRight: '5px' }}>#{index + 1}</span>{chapter}
                    </span>
                    <span style={{ flexShrink: 0 }}>
                      {stats.done || 0} / {stats.total || 0}
                      {stats.failed ? ` (${stats.failed} failed)` : ''}
                      <button className="icon-button danger" onClick={() => onDeleteChapter(task.id, chapter)} title="Cancel and delete subtask">
                        <XCircle size={14} />
                      </button>
                    </span>
                  </div>
                  <div style={{ height: '4px', background: 'rgba(255,255,255,0.1)', borderRadius: '99px', overflow: 'hidden' }}>
                    <div style={{ width: `${stats.total ? ((stats.done || 0) / stats.total) * 100 : 0}%`, height: '100%', background: '#60a5fa' }} />
                  </div>
                </div>
              ))}
              {chapters.length > visibleChapters.length && (
                <small style={{ color: 'var(--text-muted)' }}>{chapters.length - visibleChapters.length} more subtasks hidden for rendering performance.</small>
              )}
            </div>
          )}
        </div>

        <aside style={{ flexShrink: 0, textAlign: 'right' }}>
          <strong>{task.progress}%</strong>
          <p style={{ fontSize: '0.82rem', color: 'var(--text-muted)' }}>{task.status}</p>
          {totals.total > 0 && <p><Image size={13} /> {totals.done} / {totals.total}</p>}
          {totals.failed > 0 && <p style={{ color: '#f87171' }}>{totals.failed} failed</p>}
          {totals.size > 0 && <p><HardDrive size={13} /> {formatBytes(totals.size)}</p>}
          {task.status === 'downloading' && meta.eta_seconds !== undefined && <p style={{ color: '#10b981' }}><Clock size={13} /> {formatTime(meta.eta_seconds)}</p>}
          {FINISHED_STATUSES.has(task.status) && meta.total_time_seconds !== undefined && <p><Clock size={13} /> {formatTime(meta.total_time_seconds)}</p>}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '7px', marginTop: '10px' }}>
            <button className="icon-button" onClick={() => onLogs(task.id, task.title)} title="Logs"><Info size={16} /></button>
            <button className="icon-button" onClick={() => onOpen(task.id)} title="Open folder"><FolderOpen size={16} /></button>
            {task.status === 'downloading' && <button className="icon-button" onClick={() => onPause(task.id)} title="Pause"><Pause size={16} /></button>}
            {task.status === 'paused' && <button className="icon-button" onClick={() => onResume(task.id)} title="Resume"><Play size={16} /></button>}
            {FINISHED_STATUSES.has(task.status) && <button className="icon-button" onClick={() => onRestart(task.id)} title="Restart"><RotateCw size={16} /></button>}
            <button className="icon-button danger" onClick={() => onDelete(task.id)} title="Delete"><Trash2 size={16} /></button>
          </div>
        </aside>
      </div>
    </article>
  )
})


function Modal({ children, onClose, width = '500px' }) {
  return (
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="glass-panel" style={{ width, maxWidth: '92vw', maxHeight: '88vh', overflow: 'auto', padding: '2rem', background: '#0f172a' }}>
        {children}
      </div>
    </div>
  )
}


function App() {
  const [url, setUrl] = useState('')
  const [downloads, setDownloads] = useState([])
  const [visibleCount, setVisibleCount] = useState(50)
  const [connection, setConnection] = useState('Connecting')
  const [plugins, setPlugins] = useState([])
  const [showPlugins, setShowPlugins] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [deleteTaskId, setDeleteTaskId] = useState(null)
  const [logsModal, setLogsModal] = useState(null)
  const [settings, setSettings] = useState({ ui_scale: 1 })
  const [draftSettings, setDraftSettings] = useState(null)
  const downloadsRef = useRef(downloads)
  const payloadRef = useRef('')
  const scaleSaveTimer = useRef(null)

  useEffect(() => { downloadsRef.current = downloads }, [downloads])

  const loadSettings = useCallback(async () => {
    const response = await apiFetch('/settings')
    const data = await response.json()
    setSettings(data)
    setDraftSettings(data)
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => loadSettings().catch(console.error), 0)
    return () => window.clearTimeout(timer)
  }, [loadSettings])

  useEffect(() => {
    let stopped = false
    let timer
    let controller
    const poll = async () => {
      controller = new AbortController()
      let delay = document.hidden ? 5000 : 3000
      try {
        const response = await fetch(`${API_ROOT}/downloads?limit=250`, { signal: controller.signal })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const text = await response.text()
        if (text !== payloadRef.current) {
          payloadRef.current = text
          const data = JSON.parse(text)
          setDownloads(data)
          delay = data.some((task) => ACTIVE_STATUSES.has(task.status)) ? 750 : 3000
        } else if (downloadsRef.current.some((task) => ACTIVE_STATUSES.has(task.status))) {
          delay = 750
        }
        setConnection('Connected')
      } catch (error) {
        if (error.name !== 'AbortError') setConnection('Disconnected')
        delay = 3000
      } finally {
        if (!stopped) timer = window.setTimeout(poll, delay)
      }
    }
    poll()
    return () => {
      stopped = true
      window.clearTimeout(timer)
      controller?.abort()
    }
  }, [])

  const submitDownload = useCallback(async (targetUrl) => {
    if (!targetUrl) return
    const duplicate = downloadsRef.current.some((task) => task.url === targetUrl)
    if (duplicate && !window.confirm('This URL is already in your tasks. Download it again?')) return
    await apiFetch('/downloads', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: targetUrl }),
    })
    setUrl('')
  }, [])

  useEffect(() => {
    const paste = (event) => {
      if (['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)) return
      const value = event.clipboardData?.getData('Text')?.trim()
      if (value?.startsWith('http://') || value?.startsWith('https://')) {
        event.preventDefault()
        submitDownload(value).catch((error) => window.alert(error.message))
      }
    }
    window.addEventListener('paste', paste)
    return () => window.removeEventListener('paste', paste)
  }, [submitDownload])

  useEffect(() => {
    const wheel = (event) => {
      if (!event.ctrlKey) return
      event.preventDefault()
      setSettings((current) => {
        const delta = event.deltaY < 0 ? 0.1 : -0.1
        const next = { ...current, ui_scale: Math.max(0.5, Math.min(1.5, Number(((current.ui_scale || 1) + delta).toFixed(1)))) }
        setDraftSettings(next)
        window.clearTimeout(scaleSaveTimer.current)
        scaleSaveTimer.current = window.setTimeout(() => {
          apiFetch('/settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(next),
          }).catch(console.error)
        }, 350)
        return next
      })
    }
    window.addEventListener('wheel', wheel, { passive: false })
    return () => {
      window.removeEventListener('wheel', wheel)
      window.clearTimeout(scaleSaveTimer.current)
    }
  }, [])

  const postAction = useCallback((path) => apiFetch(path, { method: 'POST' }).catch((error) => window.alert(error.message)), [])
  const pause = useCallback((id) => postAction(`/downloads/${id}/pause`), [postAction])
  const resume = useCallback((id) => postAction(`/downloads/${id}/resume`), [postAction])
  const restart = useCallback((id) => postAction(`/downloads/${id}/restart`), [postAction])
  const openFolder = useCallback((id) => postAction(`/downloads/${id}/open`), [postAction])

  const deleteChapter = useCallback(async (id, chapter) => {
    if (!window.confirm(`Cancel and delete "${chapter}"?`)) return
    await apiFetch(`/downloads/${id}/items/${encodeURIComponent(chapter)}`, { method: 'DELETE' })
  }, [])

  const viewLogs = useCallback(async (id, title) => {
    setLogsModal({ title, logs: 'Loading…' })
    try {
      const response = await apiFetch(`/downloads/${id}/logs`)
      const data = await response.json()
      setLogsModal({ title, logs: data.logs })
    } catch (error) {
      setLogsModal({ title, logs: error.message })
    }
  }, [])

  const deleteTask = useCallback((id) => setDeleteTaskId(id), [])
  const visibleDownloads = downloads.slice(0, visibleCount)

  return (
    <div className="container" style={{ padding: '0 2rem 2rem', maxWidth: '1200px', margin: '0 auto', zoom: settings.ui_scale || 1 }}>
      <div className="sticky-header">
        <header className="app-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <div className="logo"><Download size={24} /></div>
            <div><h1>Ultimate Universal Downloader</h1><p>Bounded concurrent media engine</p></div>
          </div>
          <div style={{ display: 'flex', gap: '10px' }}>
            <button className="btn glass-panel" onClick={() => window.alert(`Backend: ${connection}`)}><Server size={17} /> {connection}</button>
            <button className="btn glass-panel" onClick={async () => { const response = await apiFetch('/plugins'); setPlugins((await response.json()).plugins); setShowPlugins(true) }}><Layers size={17} /> Plugins</button>
            <button className="btn glass-panel" onClick={() => { loadSettings().catch(console.error); setShowSettings(true) }}><Settings size={17} /></button>
          </div>
        </header>
        <section className="glass-panel" style={{ padding: '1.5rem' }}>
          <form onSubmit={(event) => { event.preventDefault(); submitDownload(url).catch((error) => window.alert(error.message)) }}>
            <label><Link size={19} /> Paste URL to download</label>
            <div className="input-group">
              <input className="input-field" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.com/gallery/123" />
              <button className="btn btn-primary" type="submit"><Download size={17} /> Extract</button>
            </div>
          </form>
        </section>
      </div>

      <main>
        <div className="task-heading">
          <h2><PlayCircle size={20} /> Tasks</h2>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button className="btn glass-panel" onClick={() => postAction('/downloads/pause-all')}><Pause size={14} /> Pause all</button>
            <button className="btn glass-panel" onClick={() => postAction('/downloads/resume-all')}><Play size={14} /> Resume all</button>
            <button className="btn glass-panel danger" onClick={async () => { if (window.confirm('Clear task history? Downloaded files are kept.')) await postAction('/downloads/clear-all') }}><Trash2 size={14} /> Clear all</button>
          </div>
        </div>
        <div style={{ display: 'grid', gap: '12px' }}>
          {visibleDownloads.length === 0 ? (
            <div className="glass-panel empty-state"><Search size={42} /><p>No tasks yet.</p></div>
          ) : visibleDownloads.map((task, index) => (
            <TaskCard
              key={task.id}
              task={task}
              number={downloads.length - index}
              onPause={pause}
              onResume={resume}
              onRestart={restart}
              onDelete={deleteTask}
              onOpen={openFolder}
              onLogs={viewLogs}
              onDeleteChapter={deleteChapter}
            />
          ))}
          {visibleCount < downloads.length && <button className="btn glass-panel" onClick={() => setVisibleCount((count) => count + 50)}>Show 50 more</button>}
        </div>
      </main>

      {showPlugins && <Modal onClose={() => setShowPlugins(false)}><h2>Loaded plugins</h2>{plugins.map((plugin) => <div key={plugin.name} className="modal-row"><strong>{plugin.name}</strong><small>{plugin.urls.join(', ')}</small></div>)}</Modal>}

      {showSettings && draftSettings && (
        <Modal onClose={() => setShowSettings(false)} width="460px">
          <h2>Settings</h2>
          <label>Download directory<input className="input-field" value={draftSettings.download_dir || ''} onChange={(event) => setDraftSettings({ ...draftSettings, download_dir: event.target.value })} /></label>
          <label className="check"><input type="checkbox" checked={draftSettings.use_playwright ?? true} onChange={(event) => setDraftSettings({ ...draftSettings, use_playwright: event.target.checked })} /> Allow Playwright fallback</label>
          {[
            ['max_concurrent_tasks', 'Concurrent tasks', 1, 20],
            ['max_concurrent_items', 'Workers per task', 1, 50],
            ['max_global_items', 'Global download limit', 1, 200],
            ['max_concurrent_per_host', 'Per-host download limit', 1, 50],
            ['max_extract_concurrency', 'Extraction concurrency', 1, 50],
            ['request_timeout_seconds', 'Request timeout (seconds)', 5, 300],
          ].map(([key, label, min, max]) => (
            <label key={key}>{label}<input className="input-field compact" type="number" min={min} max={max} value={draftSettings[key]} onChange={(event) => setDraftSettings({ ...draftSettings, [key]: Number(event.target.value) })} /></label>
          ))}
          <label>UI scale: {draftSettings.ui_scale || 1}×<input type="range" min="0.5" max="1.5" step="0.1" value={draftSettings.ui_scale || 1} onChange={(event) => setDraftSettings({ ...draftSettings, ui_scale: Number(event.target.value) })} /></label>
          <small>Engine concurrency changes take effect after restarting the app.</small>
          <div className="modal-actions"><button className="btn" onClick={() => setShowSettings(false)}>Cancel</button><button className="btn btn-primary" onClick={async () => { const response = await apiFetch('/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draftSettings) }); const data = await response.json(); setSettings(data.settings); setShowSettings(false) }}>Save</button></div>
        </Modal>
      )}

      {deleteTaskId && (
        <Modal onClose={() => setDeleteTaskId(null)} width="420px">
          <h2>Delete task</h2>
          <p style={{ color: 'var(--text-muted)' }}>Choose whether downloaded files should also be removed.</p>
          <div className="modal-actions vertical">
            <button className="btn" onClick={async () => { await apiFetch(`/downloads/${deleteTaskId}?delete_files=false`, { method: 'DELETE' }); setDeleteTaskId(null) }}>Keep downloaded files</button>
            <button className="btn danger" onClick={async () => { try { await apiFetch(`/downloads/${deleteTaskId}?delete_files=true`, { method: 'DELETE' }); setDeleteTaskId(null) } catch (error) { window.alert(error.message) } }}>Delete task and files</button>
          </div>
        </Modal>
      )}

      {logsModal && <Modal onClose={() => setLogsModal(null)} width="800px"><h2>{logsModal.title}</h2><pre className="log-viewer">{logsModal.logs}</pre></Modal>}
    </div>
  )
}

export default App
