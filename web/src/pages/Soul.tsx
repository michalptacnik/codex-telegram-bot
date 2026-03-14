import { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi';

interface SoulProfile {
  name: string;
  voice: string;
  principles: string[];
  boundaries: string[];
  style: {
    emoji: string;
    emphasis: string;
    brevity: string;
  };
}

export default function Soul() {
  const { fetchApi } = useApi();
  const [profile, setProfile] = useState<SoulProfile | null>(null);
  const [rendered, setRendered] = useState('');
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  useEffect(() => {
    fetchApi('/api/soul').then((data) => {
      if (data?.profile) {
        setProfile(data.profile);
        setRendered(data.rendered || '');
      }
    });
  }, []);

  const handleSave = async () => {
    if (!profile) return;
    setSaving(true);
    setError('');
    setSuccess('');
    try {
      const res = await fetchApi('/api/soul', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(profile),
      });
      if (res?.ok) {
        setSuccess('Soul profile saved!');
        setEditing(false);
      } else if (res?.error) {
        setError(res.error);
      }
    } catch (e) {
      setError('Failed to save');
    } finally {
      setSaving(false);
    }
  };

  if (!profile) {
    return <div className="p-6 text-gray-400">Loading soul profile...</div>;
  }

  return (
    <div className="p-6 max-w-4xl">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-white">Soul / Personality</h1>
        <button
          onClick={() => editing ? handleSave() : setEditing(true)}
          disabled={saving}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white rounded-lg"
        >
          {saving ? 'Saving...' : editing ? 'Save' : 'Edit'}
        </button>
      </div>

      {error && <div className="mb-4 p-3 bg-red-900/50 border border-red-700 rounded-lg text-red-300">{error}</div>}
      {success && <div className="mb-4 p-3 bg-green-900/50 border border-green-700 rounded-lg text-green-300">{success}</div>}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Identity */}
        <div className="bg-gray-900 rounded-xl p-6 border border-gray-800">
          <h2 className="text-lg font-semibold text-white mb-4">Identity</h2>
          <div className="space-y-4">
            <div>
              <label className="block text-sm text-gray-400 mb-1">Name</label>
              {editing ? (
                <input value={profile.name} onChange={(e) => setProfile({...profile, name: e.target.value})}
                  className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white" />
              ) : (
                <p className="text-white">{profile.name}</p>
              )}
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">Voice</label>
              {editing ? (
                <input value={profile.voice} onChange={(e) => setProfile({...profile, voice: e.target.value})}
                  className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white" />
              ) : (
                <p className="text-white">{profile.voice}</p>
              )}
            </div>
          </div>
        </div>

        {/* Style */}
        <div className="bg-gray-900 rounded-xl p-6 border border-gray-800">
          <h2 className="text-lg font-semibold text-white mb-4">Style</h2>
          <div className="space-y-4">
            {(['emoji', 'emphasis', 'brevity'] as const).map((key) => (
              <div key={key}>
                <label className="block text-sm text-gray-400 mb-1 capitalize">{key}</label>
                {editing ? (
                  <select value={profile.style[key]}
                    onChange={(e) => setProfile({...profile, style: {...profile.style, [key]: e.target.value}})}
                    className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white">
                    {key === 'emoji' && ['off', 'light', 'on'].map(v => <option key={v} value={v}>{v}</option>)}
                    {key === 'emphasis' && ['plain', 'light', 'rich'].map(v => <option key={v} value={v}>{v}</option>)}
                    {key === 'brevity' && ['short', 'normal'].map(v => <option key={v} value={v}>{v}</option>)}
                  </select>
                ) : (
                  <p className="text-white">{profile.style[key]}</p>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Principles */}
        <div className="bg-gray-900 rounded-xl p-6 border border-gray-800">
          <h2 className="text-lg font-semibold text-white mb-4">Principles</h2>
          <ul className="space-y-2">
            {profile.principles.map((p, i) => (
              <li key={i} className="flex items-start gap-2">
                <span className="text-blue-400 mt-0.5">-</span>
                {editing ? (
                  <input value={p} onChange={(e) => {
                    const newP = [...profile.principles];
                    newP[i] = e.target.value;
                    setProfile({...profile, principles: newP});
                  }} className="flex-1 px-3 py-1 bg-gray-800 border border-gray-700 rounded text-white text-sm" />
                ) : (
                  <span className="text-gray-300 text-sm">{p}</span>
                )}
              </li>
            ))}
          </ul>
        </div>

        {/* Boundaries */}
        <div className="bg-gray-900 rounded-xl p-6 border border-gray-800">
          <h2 className="text-lg font-semibold text-white mb-4">Boundaries</h2>
          <ul className="space-y-2">
            {profile.boundaries.map((b, i) => (
              <li key={i} className="flex items-start gap-2">
                <span className="text-red-400 mt-0.5">-</span>
                {editing ? (
                  <input value={b} onChange={(e) => {
                    const newB = [...profile.boundaries];
                    newB[i] = e.target.value;
                    setProfile({...profile, boundaries: newB});
                  }} className="flex-1 px-3 py-1 bg-gray-800 border border-gray-700 rounded text-white text-sm" />
                ) : (
                  <span className="text-gray-300 text-sm">{b}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      </div>

      {/* Raw view */}
      <div className="mt-6 bg-gray-900 rounded-xl p-6 border border-gray-800">
        <h2 className="text-lg font-semibold text-white mb-4">Raw SOUL.md</h2>
        <pre className="text-gray-300 text-sm font-mono whitespace-pre-wrap">{rendered}</pre>
      </div>
    </div>
  );
}
