import { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi';

interface Mission {
  id: string;
  goal: string;
  state: string;
  created_at: string;
  updated_at: string;
  completed_at?: string;
  budget_usd: number;
  spent_usd: number;
  max_steps: number;
  error?: string;
  plan?: {
    goal: string;
    steps: Array<{
      index: number;
      description: string;
      status: string;
      output: string;
    }>;
  };
}

const stateColors: Record<string, string> = {
  idle: 'bg-gray-600',
  running: 'bg-blue-600',
  paused: 'bg-yellow-600',
  completed: 'bg-green-600',
  failed: 'bg-red-600',
  blocked: 'bg-orange-600',
};

export default function Missions() {
  const { fetchApi } = useApi();
  const [missions, setMissions] = useState<Mission[]>([]);
  const [newGoal, setNewGoal] = useState('');
  const [creating, setCreating] = useState(false);

  const loadMissions = async () => {
    const data = await fetchApi('/api/missions');
    if (data?.missions) setMissions(data.missions);
  };

  useEffect(() => { loadMissions(); }, []);

  const createMission = async () => {
    if (!newGoal.trim()) return;
    setCreating(true);
    await fetchApi('/api/missions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal: newGoal }),
    });
    setNewGoal('');
    setCreating(false);
    loadMissions();
  };

  const controlMission = async (id: string, action: string) => {
    await fetchApi(`/api/missions/${id}/${action}`, { method: 'POST' });
    loadMissions();
  };

  return (
    <div className="p-6 max-w-6xl">
      <h1 className="text-2xl font-bold text-white mb-6">Missions</h1>

      {/* Create Mission */}
      <div className="bg-gray-900 rounded-xl p-6 border border-gray-800 mb-6">
        <h2 className="text-lg font-semibold text-white mb-4">New Mission</h2>
        <div className="flex gap-4">
          <input
            value={newGoal}
            onChange={(e) => setNewGoal(e.target.value)}
            placeholder="Enter mission goal..."
            className="flex-1 px-4 py-3 bg-gray-800 border border-gray-700 rounded-lg text-white"
            onKeyDown={(e) => e.key === 'Enter' && createMission()}
          />
          <button
            onClick={createMission}
            disabled={creating || !newGoal.trim()}
            className="px-6 py-3 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white rounded-lg"
          >
            {creating ? 'Creating...' : 'Create'}
          </button>
        </div>
      </div>

      {/* Mission List */}
      <div className="space-y-4">
        {missions.length === 0 ? (
          <div className="bg-gray-900 rounded-xl p-6 border border-gray-800 text-gray-400 text-center">
            No missions yet. Create one above.
          </div>
        ) : missions.map((mission) => (
          <div key={mission.id} className="bg-gray-900 rounded-xl p-6 border border-gray-800">
            <div className="flex items-start justify-between mb-4">
              <div>
                <h3 className="text-lg font-semibold text-white">{mission.goal}</h3>
                <p className="text-sm text-gray-400 mt-1">
                  ID: {mission.id.slice(0, 8)}... | Created: {new Date(mission.created_at).toLocaleString()}
                </p>
              </div>
              <div className="flex items-center gap-3">
                <span className={`px-3 py-1 rounded-full text-xs font-medium text-white ${stateColors[mission.state] || 'bg-gray-600'}`}>
                  {mission.state}
                </span>
                {mission.state === 'running' && (
                  <>
                    <button onClick={() => controlMission(mission.id, 'pause')} className="px-3 py-1 bg-yellow-600 hover:bg-yellow-700 text-white rounded text-sm">Pause</button>
                    <button onClick={() => controlMission(mission.id, 'stop')} className="px-3 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-sm">Stop</button>
                  </>
                )}
                {mission.state === 'paused' && (
                  <button onClick={() => controlMission(mission.id, 'resume')} className="px-3 py-1 bg-green-600 hover:bg-green-700 text-white rounded text-sm">Resume</button>
                )}
              </div>
            </div>

            {/* Budget */}
            <div className="flex gap-6 mb-4 text-sm">
              <span className="text-gray-400">Budget: <span className="text-white">${mission.budget_usd.toFixed(2)}</span></span>
              <span className="text-gray-400">Spent: <span className="text-white">${mission.spent_usd.toFixed(4)}</span></span>
              <span className="text-gray-400">Max steps: <span className="text-white">{mission.max_steps}</span></span>
            </div>

            {/* Steps */}
            {mission.plan && mission.plan.steps.length > 0 && (
              <div className="mt-4">
                <h4 className="text-sm font-medium text-gray-400 mb-2">Steps</h4>
                <div className="space-y-2">
                  {mission.plan.steps.map((step) => (
                    <div key={step.index} className="flex items-center gap-3 text-sm">
                      <span className={`w-2 h-2 rounded-full ${
                        step.status === 'completed' ? 'bg-green-500' :
                        step.status === 'running' ? 'bg-blue-500' :
                        step.status === 'failed' ? 'bg-red-500' :
                        'bg-gray-600'
                      }`} />
                      <span className="text-gray-300">{step.index + 1}. {step.description}</span>
                      <span className="text-gray-500 text-xs">{step.status}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {mission.error && (
              <div className="mt-4 p-3 bg-red-900/30 border border-red-800 rounded text-red-300 text-sm">
                {mission.error}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
