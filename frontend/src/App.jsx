import { useState, useEffect, useRef } from 'react'
import { Download, PlayCircle, Settings, Layers, Search, Server, Link, X, Pause, Play, Trash2, FolderOpen, Image, HardDrive, Info, RotateCw, XCircle, Clock } from 'lucide-react'
import './index.css'

const formatBytes = (bytes) => {
  if (!bytes) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

const formatTime = (seconds) => {
  if (seconds === undefined || seconds === null || isNaN(seconds)) return '0s';
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${rm}m`;
}

function App() {
  const [url, setUrl] = useState('')
  const [downloads, setDownloads] = useState([])
  const downloadsRef = useRef(downloads)
  useEffect(() => {
    downloadsRef.current = downloads
  }, [downloads])
  const [status, setStatus] = useState('Idle')
  const [showPlugins, setShowPlugins] = useState(false)
  const [pluginsList, setPluginsList] = useState([])
  const [showSettings, setShowSettings] = useState(false)
  const [deleteModal, setDeleteModal] = useState({ show: false, taskId: null })
  const [logsModal, setLogsModal] = useState({ show: false, taskId: null, title: "", logs: "Loading logs..." })
  const [settings, setSettings] = useState({ download_dir: '', dark_mode: true, use_playwright: true, ui_scale: 1.0 })
  const [draftSettings, setDraftSettings] = useState(null)

  const fetchSettings = async () => {
    try {
      const res = await fetch('http://127.0.0.1:8000/api/settings');
      if (res.ok) {
        const data = await res.json();
        setSettings(data);
        setDraftSettings(data);
      }
    } catch (err) {
      console.error("Failed to fetch settings", err);
    }
  }

  const saveSettings = async (newSettings) => {
    try {
      await fetch('http://127.0.0.1:8000/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newSettings)
      });
      setSettings(newSettings);
    } catch (err) {
      console.error("Failed to save settings", err);
    }
  }

  const fetchPlugins = async () => {
    try {
      const res = await fetch('http://127.0.0.1:8000/api/plugins');
      if (res.ok) {
        const data = await res.json();
        setPluginsList(data.plugins || []);
      }
    } catch (err) {
      console.error("Failed to fetch plugins", err);
    }
  }

  useEffect(() => {
    // Poll the backend every second to update tasks
    const interval = setInterval(async () => {
      try {
        const res = await fetch('http://127.0.0.1:8000/api/downloads');
        if (res.ok) {
          const data = await res.json();
          setDownloads(data);
          setStatus('Connected');
        } else {
          setStatus('Error');
        }
      } catch (err) {
        setStatus('Disconnected');
      }
    }, 1000);
    return () => clearInterval(interval);
  }, []);

  const submitDownload = async (targetUrl) => {
    const isDuplicate = downloadsRef.current.some(dl => dl.url === targetUrl);
    if (isDuplicate) {
      if (!window.confirm("This URL is already in your tasks. Do you want to download it again?")) {
        return;
      }
    }
    
    try {
      await fetch('http://127.0.0.1:8000/api/downloads', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: targetUrl })
      });
      setUrl('');
    } catch (err) {
      console.error("Failed to add download", err);
    }
  }

  useEffect(() => {
    const handleGlobalPaste = async (e) => {
      const clipboardData = e.clipboardData || window.clipboardData;
      const pastedData = clipboardData.getData('Text');
      
      if (pastedData) {
        const urlMatch = pastedData.trim();
        if (urlMatch.startsWith('http://') || urlMatch.startsWith('https://')) {
          
          // Don't hijack if user is pasting into any input fields (like the URL field or settings)
          if (document.activeElement.tagName === 'INPUT' || document.activeElement.tagName === 'TEXTAREA') {
            return;
          }
          
          e.preventDefault();
          submitDownload(urlMatch);
        }
      }
    };

    window.addEventListener('paste', handleGlobalPaste);
    return () => window.removeEventListener('paste', handleGlobalPaste);
  }, []);

  const handleDownload = async (e) => {
    e.preventDefault();
    if (!url) return;
    submitDownload(url);
  }

  const handlePause = async (id) => {
    try {
      await fetch(`http://127.0.0.1:8000/api/downloads/${id}/pause`, { method: 'POST' });
    } catch (err) { console.error(err); }
  }

  const handleResume = async (id) => {
    try {
      await fetch(`http://127.0.0.1:8000/api/downloads/${id}/resume`, { method: 'POST' });
    } catch (err) { console.error(err); }
  }

  const handleRestart = async (id) => {
    try {
      await fetch(`http://127.0.0.1:8000/api/downloads/${id}/restart`, { method: 'POST' });
    } catch (err) { console.error(err); }
  }

  const handlePauseAll = async () => {
    try {
      await fetch('http://127.0.0.1:8000/api/downloads/pause-all', { method: 'POST' });
    } catch (err) { console.error(err); }
  }

  const handleResumeAll = async () => {
    try {
      await fetch('http://127.0.0.1:8000/api/downloads/resume-all', { method: 'POST' });
    } catch (err) { console.error(err); }
  }

  const handleClearAll = async () => {
    if (window.confirm("Are you sure you want to clear all tasks from the list? (Downloaded files will not be deleted)")) {
      try {
        await fetch('http://127.0.0.1:8000/api/downloads/clear-all', { method: 'POST' });
      } catch (err) { console.error(err); }
    }
  }

  const handleDelete = async (id, deleteFiles) => {
    try {
      await fetch(`http://127.0.0.1:8000/api/downloads/${id}?delete_files=${deleteFiles}`, { method: 'DELETE' });
    } catch (err) { console.error(err); }
  }

  const handleOpenFolder = async (id) => {
    try {
      await fetch(`http://127.0.0.1:8000/api/downloads/${id}/open`, { method: 'POST' });
    } catch (err) { console.error(err); }
  }

  const handleDeleteChapter = async (taskId, chapter) => {
    if (window.confirm(`Are you sure you want to cancel and delete "${chapter}"?`)) {
      try {
        await fetch(`http://127.0.0.1:8000/api/downloads/${taskId}/items/${encodeURIComponent(chapter)}`, { method: 'DELETE' });
      } catch (err) {
        console.error(err);
      }
    }
  }

  const handleViewLogs = async (id, title) => {
    setLogsModal({ show: true, taskId: id, title: title, logs: "Loading logs..." });
    try {
      const res = await fetch(`http://127.0.0.1:8000/api/downloads/${id}/logs`);
      const data = await res.json();
      setLogsModal({ show: true, taskId: id, title: title, logs: data.logs });
    } catch (err) {
      setLogsModal({ show: true, taskId: id, title: title, logs: "Failed to fetch logs." });
    }
  }

  return (
    <div className="container" style={{ padding: '0 2rem 2rem', maxWidth: '1200px', margin: '0 auto', zoom: settings.ui_scale || 1.0 }}>
      <div style={{ position: 'sticky', top: 0, zIndex: 50, background: 'rgba(15, 23, 42, 0.95)', backdropFilter: 'blur(16px)', WebkitBackdropFilter: 'blur(16px)', padding: '2rem 0 1rem', margin: '0 0 1rem' }}>
        <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '2rem' }} className="animate-fade-in">
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <div style={{ background: 'var(--accent-primary)', padding: '10px', borderRadius: '12px', display: 'flex' }}>
            <Download size={24} color="white" />
          </div>
          <div>
            <h1 style={{ fontSize: '1.5rem', fontWeight: 600 }}>Ultimate Universal Downloader (UUD)</h1>
            <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem' }}>Extreme Concurrency & Anti-Bot Engine</p>
          </div>
        </div>
        
        <div style={{ display: 'flex', gap: '16px' }}>
          <button className="btn glass-panel" style={{ background: 'rgba(255, 255, 255, 0.1)', color: 'white' }} onClick={() => alert(`Backend Status: ${status}`)}>
            <Server size={18} /> Backend: {status}
          </button>
          <button className="btn glass-panel" style={{ background: 'rgba(255, 255, 255, 0.1)', color: 'white' }} onClick={() => { fetchPlugins(); setShowPlugins(true); }}>
            <Layers size={18} /> Plugins
          </button>
          <button className="btn glass-panel" style={{ background: 'rgba(255, 255, 255, 0.1)', color: 'white' }} onClick={() => { fetchSettings(); setShowSettings(true); }}>
            <Settings size={18} />
          </button>
        </div>
        </header>

        <section className="glass-panel animate-fade-in" style={{ padding: '2rem', animationDelay: '0.1s' }}>
          <form onSubmit={handleDownload} style={{ display: 'flex', gap: '16px', flexDirection: 'column' }}>
            <label style={{ fontSize: '1.1rem', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Link size={20} color="var(--accent-primary)" />
              Paste URL to Download
            </label>
            <div className="input-group">
              <input 
                type="text" 
                className="input-field" 
                placeholder="https://example.com/gallery/123..."
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
              <button type="submit" className="btn btn-primary" style={{ padding: '0 32px' }}>
                <Download size={18} /> Extract
              </button>
            </div>
            <div style={{ display: 'flex', gap: '12px', marginTop: '8px' }}>
              <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)', background: 'rgba(0,0,0,0.2)', padding: '4px 12px', borderRadius: '100px' }}>Supports: Images, Videos, HLS</span>
              <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)', background: 'rgba(0,0,0,0.2)', padding: '4px 12px', borderRadius: '100px' }}>Bypass: Cloudflare, Turnstile</span>
            </div>
          </form>
        </section>
      </div>

      <main>
        <section>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
            <h2 style={{ fontSize: '1.2rem', margin: 0, display: 'flex', alignItems: 'center', gap: '8px' }}>
              <PlayCircle size={20} color="var(--text-muted)" /> Active Tasks
            </h2>
            <div style={{ display: 'flex', gap: '12px' }}>
              <button className="btn btn-secondary glass-panel" style={{ background: 'rgba(255, 255, 255, 0.1)', padding: '6px 12px', fontSize: '0.9rem', color: 'white' }} onClick={handlePauseAll}>
                <Pause size={14} /> Stop All
              </button>
              <button className="btn btn-secondary glass-panel" style={{ background: 'rgba(255, 255, 255, 0.1)', padding: '6px 12px', fontSize: '0.9rem', color: 'white' }} onClick={handleResumeAll}>
                <Play size={14} /> Resume All
              </button>
              <button className="btn btn-secondary glass-panel" style={{ background: 'rgba(255, 255, 255, 0.1)', padding: '6px 12px', fontSize: '0.9rem', color: '#ef4444' }} onClick={handleClearAll}>
                <Trash2 size={14} /> Clear All
              </button>
            </div>
          </div>
          
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {downloads.length === 0 ? (
              <div className="glass-panel" style={{ padding: '3rem', textAlign: 'center', color: 'var(--text-muted)' }}>
                <Search size={48} style={{ opacity: 0.2, margin: '0 auto 1rem' }} />
                <p>No active downloads. Paste a link above to start.</p>
              </div>
            ) : (
              downloads.map((dl, idx) => {
                let totalItems = 0;
                let doneItems = 0;
                let totalSize = 0;
                let meta = null;
                if (dl.details && dl.details !== "{}") {
                  Object.entries(JSON.parse(dl.details)).forEach(([key, s]) => {
                    if (key === '_meta') {
                      meta = s;
                    } else if (key !== '_cancelled') {
                      totalItems += s.total || 0;
                      doneItems += s.done || 0;
                      totalSize += s.size_bytes || 0;
                    }
                  });
                }
                
                let borderColor = 'rgba(255,255,255,0.05)'; // default glass panel border
                if (dl.status === 'completed') {
                  borderColor = doneItems < totalItems ? 'rgba(234, 179, 8, 0.6)' : 'rgba(16, 185, 129, 0.6)'; // yellow : green
                } else if (dl.status === 'extracting') {
                  borderColor = 'rgba(59, 130, 246, 0.6)'; // blue
                } else if (dl.status === 'error' || dl.status === 'failed') {
                  borderColor = 'rgba(239, 68, 68, 0.6)'; // red
                }

                return (
                <div key={dl.id} className="glass-panel" style={{ padding: '1.5rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px', border: `3px solid ${borderColor}`, transition: 'border-color 0.3s ease' }}>
                  {dl.thumbnail && (
                    <div style={{ flexShrink: 0, width: '120px', height: '160px', borderRadius: '8px', overflow: 'hidden', boxShadow: '0 4px 6px rgba(0,0,0,0.3)' }}>
                      <img src={`http://127.0.0.1:8000/api/proxy?url=${encodeURIComponent(dl.thumbnail)}&referer=${encodeURIComponent(dl.url)}`} alt="Thumbnail" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                    </div>
                  )}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <h3 style={{ fontWeight: 500, marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      <span style={{ color: 'var(--text-muted)', fontSize: '0.9rem', marginRight: '4px', flexShrink: 0 }}>#{downloads.length - idx}</span>
                      <img src={`https://www.google.com/s2/favicons?domain=${dl.url}&sz=32`} alt="icon" style={{ width: '18px', height: '18px', borderRadius: '4px', flexShrink: 0 }} onError={(e) => e.target.style.display = 'none'} />
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{dl.title}</span>
                    </h3>
                    <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginBottom: '12px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {dl.details && dl.details !== "{}" ? <span style={{ color: '#60a5fa' }}>{Object.keys(JSON.parse(dl.details)).filter(k => k !== '_cancelled' && k !== '_meta').length} items detected • </span> : ''}
                      <a href={dl.url} target="_blank" rel="noopener noreferrer" style={{ color: 'inherit', textDecoration: 'none' }} onMouseEnter={e => e.target.style.textDecoration='underline'} onMouseLeave={e => e.target.style.textDecoration='none'}>
                        {dl.url}
                      </a>
                    </p>
                    
                    {/* Main Progress Bar */}
                    <div style={{ height: '6px', background: 'rgba(255,255,255,0.1)', borderRadius: '100px', overflow: 'hidden' }}>
                      <div style={{ width: `${dl.progress}%`, height: '100%', background: 'var(--accent-primary)', transition: 'width 0.3s ease' }}></div>
                    </div>
                    
                    {/* Itemized Progress */}
                    {dl.details && dl.details !== "{}" && (
                      <div style={{ marginTop: '16px', display: 'flex', flexDirection: 'column', gap: '10px', maxHeight: '150px', overflowY: 'auto', paddingRight: '8px' }}>
                        {Object.entries(JSON.parse(dl.details))
                          .filter(([chapter]) => chapter !== '_cancelled' && chapter !== '_meta')
                          .map(([chapter, stats], idx) => (
                          <div key={chapter} style={{ fontSize: '0.8rem' }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
                              <span style={{ color: 'var(--text-muted)' }}>
                                <span style={{ marginRight: '6px', opacity: 0.5 }}>#{idx + 1}</span>
                                {chapter}
                              </span>
                              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                <span style={{ color: 'var(--text-muted)' }}>{stats.done} / {stats.total}</span>
                                <button 
                                  className="btn" 
                                  style={{ padding: '0', color: '#ef4444', background: 'transparent', border: 'none', cursor: 'pointer' }}
                                  onClick={() => handleDeleteChapter(dl.id, chapter)}
                                  title="Cancel & Delete Item"
                                >
                                  <XCircle size={14} />
                                </button>
                              </div>
                            </div>
                            <div style={{ height: '4px', background: 'rgba(255,255,255,0.1)', borderRadius: '100px', overflow: 'hidden' }}>
                              <div style={{ height: '100%', background: '#60a5fa', width: `${(stats.done / stats.total) * 100}%`, transition: 'width 0.3s ease' }} />
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                  <div style={{ paddingLeft: '2rem', textAlign: 'right', display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '8px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '4px' }}>
                      <span style={{ fontSize: '1.2rem', fontWeight: 600 }}>{dl.progress}%</span>
                      <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>{dl.status}</p>
                    </div>

                    {(totalItems > 0 || totalSize > 0 || meta) ? (
                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '4px', marginBottom: '8px' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.85rem', color: (dl.status === 'completed' && doneItems < totalItems) ? '#ef4444' : 'var(--text-muted)' }}>
                            <Image size={14} /> {doneItems} / {totalItems} items
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                            <HardDrive size={14} /> {formatBytes(totalSize)}
                          </div>
                          {meta && meta.eta_seconds !== undefined && dl.status === 'downloading' && (
                            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.85rem', color: '#10b981' }}>
                              <Clock size={14} /> ETA: {formatTime(meta.eta_seconds)}
                            </div>
                          )}
                          {meta && meta.total_time_seconds !== undefined && ['completed', 'error'].includes(dl.status) && (
                            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.85rem', color: '#10b981' }}>
                              <Clock size={14} /> Time: {formatTime(meta.total_time_seconds)}
                            </div>
                          )}
                        </div>
                      ) : null}
                    
                    <div style={{ display: 'flex', gap: '8px' }}>
                      <button className="btn glass-panel" style={{ padding: '6px', color: '#10b981' }} onClick={() => handleViewLogs(dl.id, dl.title)} title="View Logs">
                        <Info size={16} />
                      </button>
                      
                      <button className="btn glass-panel" style={{ padding: '6px', color: '#60a5fa' }} onClick={() => handleOpenFolder(dl.id)} title="Open Folder">
                        <FolderOpen size={16} />
                      </button>
                      
                      {dl.status === 'downloading' && (
                        <button className="btn glass-panel" style={{ padding: '6px' }} onClick={() => handlePause(dl.id)}>
                          <Pause size={16} />
                        </button>
                      )}
                      {dl.status === 'paused' && (
                        <button className="btn glass-panel" style={{ padding: '6px', color: 'var(--accent-primary)' }} onClick={() => handleResume(dl.id)} title="Resume">
                          <Play size={16} />
                        </button>
                      )}
                      
                      {['completed', 'error', 'failed'].includes(dl.status) && (
                        <button className="btn glass-panel" style={{ padding: '6px', color: 'var(--accent-primary)' }} onClick={() => handleRestart(dl.id)} title="Restart Task (Skips existing files)">
                          <RotateCw size={16} />
                        </button>
                      )}
                      
                      <button className="btn glass-panel" style={{ padding: '6px', color: '#ef4444' }} onClick={() => {
                        setDeleteModal({ show: true, taskId: dl.id });
                      }}>
                        <Trash2 size={16} />
                      </button>
                    </div>
                  </div>

                </div>
              )})
            )}
          </div>
        </section>
      </main>

      {/* Plugins Modal */}
      {showPlugins && (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.7)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
          <div className="glass-panel" style={{ width: '500px', padding: '2rem', background: '#0f172a' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem' }}>
              <h2 style={{ fontSize: '1.2rem', display: 'flex', alignItems: 'center', gap: '8px' }}><Layers size={20} /> Loaded Plugins</h2>
              <button className="btn" style={{ padding: '8px' }} onClick={() => setShowPlugins(false)}><X size={20} /></button>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', maxHeight: '400px', overflowY: 'auto' }}>
              {pluginsList.map((p, idx) => (
                <div key={idx} style={{ background: 'rgba(255,255,255,0.05)', padding: '12px', borderRadius: '8px', display: 'flex', alignItems: 'center', gap: '12px' }}>
                  {p.urls && p.urls.length > 0 && (
                    <img src={`https://www.google.com/s2/favicons?domain=${p.urls[0]}&sz=32`} alt="favicon" style={{ width: '32px', height: '32px', borderRadius: '4px' }} />
                  )}
                  <div>
                    <h3 style={{ fontWeight: 600, color: 'var(--accent-primary)' }}>{p.name}</h3>
                    <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>Supports: {p.urls.join(', ')}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Delete Modal */}
      {deleteModal.show && (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.7)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
          <div className="glass-panel" style={{ width: '400px', padding: '2rem', background: '#0f172a' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem' }}>
              <h2 style={{ fontSize: '1.2rem', display: 'flex', alignItems: 'center', gap: '8px', color: '#ef4444' }}><Trash2 size={20} /> Delete Task</h2>
              <button className="btn" style={{ padding: '8px' }} onClick={() => setDeleteModal({ show: false, taskId: null })}><X size={20} /></button>
            </div>
            <div style={{ color: 'var(--text-muted)', marginBottom: '2rem' }}>
              <p>How would you like to delete this task?</p>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              <button className="btn glass-panel" style={{ width: '100%', justifyContent: 'center', color: '#ef4444' }} onClick={() => {
                handleDelete(deleteModal.taskId, false);
                setDeleteModal({ show: false, taskId: null });
              }}>
                Delete Task Only (Keep Files)
              </button>
              <button className="btn btn-primary" style={{ width: '100%', justifyContent: 'center', background: '#ef4444', color: 'white' }} onClick={() => {
                handleDelete(deleteModal.taskId, true);
                setDeleteModal({ show: false, taskId: null });
              }}>
                Delete Task AND All Files
              </button>
              <button className="btn" style={{ width: '100%', justifyContent: 'center' }} onClick={() => setDeleteModal({ show: false, taskId: null })}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Settings Modal */}
      {showSettings && (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.7)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
          <div className="glass-panel" style={{ width: '400px', padding: '2rem', background: '#0f172a' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem' }}>
              <h2 style={{ fontSize: '1.2rem', display: 'flex', alignItems: 'center', gap: '8px' }}><Settings size={20} /> Settings</h2>
              <button className="btn" style={{ padding: '8px' }} onClick={() => setShowSettings(false)}><X size={20} /></button>
            </div>
            <div style={{ color: 'var(--text-muted)' }}>
              <p>Ultimate Universal Downloader (UUD) Version 1.0.0</p>
              <br />
              <div style={{ marginBottom: '1rem' }}>
                <label style={{ display: 'block', marginBottom: '8px', color: 'white' }}>Download Directory:</label>
                <input 
                  type="text" 
                  className="input-field" 
                  style={{ width: '100%', padding: '8px', borderRadius: '4px' }}
                  value={draftSettings?.download_dir || ''}
                  onChange={(e) => setDraftSettings({...draftSettings, download_dir: e.target.value})}
                />
              </div>
              <label style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                <input type="checkbox" checked={draftSettings?.dark_mode ?? true} onChange={(e) => setDraftSettings({...draftSettings, dark_mode: e.target.checked})} /> Dark Mode
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px' }}>
                <input type="checkbox" checked={draftSettings?.use_playwright ?? true} onChange={(e) => setDraftSettings({...draftSettings, use_playwright: e.target.checked})} /> Use Cloudflare Bypass (Playwright)
              </label>
              
              <div style={{ marginBottom: '12px' }}>
                <label style={{ display: 'block', marginBottom: '4px', color: 'white', fontSize: '0.9rem' }}>Max Concurrent Tasks:</label>
                <input 
                  type="number" 
                  min="1" max="20"
                  className="input-field" 
                  style={{ width: '100px', padding: '6px', borderRadius: '4px' }}
                  value={draftSettings?.max_concurrent_tasks || 3}
                  onChange={(e) => setDraftSettings({...draftSettings, max_concurrent_tasks: parseInt(e.target.value) || 1})}
                />
              </div>

              <div style={{ marginBottom: '8px' }}>
                <label style={{ display: 'block', marginBottom: '4px', color: 'white', fontSize: '0.9rem' }}>Max Concurrent Items (per task):</label>
                <input 
                  type="number" 
                  min="1" max="50"
                  className="input-field" 
                  style={{ width: '100px', padding: '6px', borderRadius: '4px' }}
                  value={draftSettings?.max_concurrent_items || 5}
                  onChange={(e) => setDraftSettings({...draftSettings, max_concurrent_items: parseInt(e.target.value) || 1})}
                />
              </div>

              <div style={{ marginBottom: '8px' }}>
                <label style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px', color: 'white', fontSize: '0.9rem' }}>
                  <span>UI Scale: {draftSettings?.ui_scale || 1.0}x</span>
                  <span style={{color: 'var(--text-muted)'}}>(Requires refresh)</span>
                </label>
                <input 
                  type="range" 
                  min="0.5" max="1.5" step="0.1"
                  style={{ width: '100%', cursor: 'pointer' }}
                  value={draftSettings?.ui_scale || 1.0}
                  onChange={(e) => setDraftSettings({...draftSettings, ui_scale: parseFloat(e.target.value) || 1.0})}
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px', marginTop: '16px' }}>
                <button className="btn" onClick={() => setShowSettings(false)}>Cancel</button>
                <button className="btn btn-primary" onClick={() => {
                  saveSettings(draftSettings);
                  setShowSettings(false);
                }}>Save Settings</button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Logs Modal */}
      {logsModal.show && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 100, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(4px)' }} onMouseDown={(e) => { if(e.target === e.currentTarget) setLogsModal({ show: false, taskId: null, title: "", logs: "" }) }}>
          <div className="glass-panel" style={{ width: '90%', maxWidth: '800px', height: '80vh', padding: '24px', display: 'flex', flexDirection: 'column', animation: 'scale-in 0.2s ease-out' }} onClick={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
              <h3 style={{ fontSize: '1.2rem', margin: 0 }}>Task Logs: {logsModal.title}</h3>
              <button className="btn" style={{ background: 'transparent', padding: '4px' }} onClick={() => setLogsModal({ show: false, taskId: null, title: "", logs: "" })}>
                <X size={20} />
              </button>
            </div>
            
            <div style={{ flex: 1, background: '#0f172a', borderRadius: '8px', padding: '16px', overflowY: 'auto', fontFamily: 'monospace', fontSize: '0.85rem', color: '#a7f3d0', whiteSpace: 'pre-wrap', border: '1px solid rgba(255,255,255,0.1)', boxShadow: 'inset 0 2px 10px rgba(0,0,0,0.5)', userSelect: 'text', WebkitUserSelect: 'text', cursor: 'text' }}>
              {logsModal.logs}
            </div>
            
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '16px' }}>
              <button className="btn" onClick={() => setLogsModal({ show: false, taskId: null, title: "", logs: "" })}>Close</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default App
