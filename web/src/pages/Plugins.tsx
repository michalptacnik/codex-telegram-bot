import { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi';

interface Plugin {
  plugin_id: string;
  name: string;
  version: string;
  capabilities: string[];
  enabled: boolean;
  trust_status: string;
  created_at: string;
  updated_at: string;
}

export default function Plugins() {
  const { fetchApi } = useApi();
  const [plugins, setPlugins] = useState<Plugin[]>([]);

  const loadPlugins = async () => {
    const data = await fetchApi('/api/plugins');
    if (data?.plugins) setPlugins(data.plugins);
  };

  useEffect(() => { loadPlugins(); }, []);

  const togglePlugin = async (id: string, enabled: boolean) => {
    const action = enabled ? 'disable' : 'enable';
    await fetchApi(`/api/plugins/${id}/${action}`, { method: 'POST' });
    loadPlugins();
  };

  const uninstallPlugin = async (id: string) => {
    if (!confirm(`Uninstall plugin "${id}"?`)) return;
    await fetchApi(`/api/plugins/${id}`, { method: 'DELETE' });
    loadPlugins();
  };

  return (
    <div className="p-6 max-w-4xl">
      <h1 className="text-2xl font-bold text-white mb-6">Plugins</h1>

      {plugins.length === 0 ? (
        <div className="bg-gray-900 rounded-xl p-8 border border-gray-800 text-center text-gray-400">
          No plugins installed. Install plugins via the CLI.
        </div>
      ) : (
        <div className="space-y-4">
          {plugins.map((plugin) => (
            <div key={plugin.plugin_id} className="bg-gray-900 rounded-xl p-6 border border-gray-800">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-lg font-semibold text-white">{plugin.name}</h3>
                  <p className="text-sm text-gray-400 mt-1">
                    v{plugin.version} | {plugin.trust_status} | {plugin.capabilities.join(', ') || 'none'}
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <button
                    onClick={() => togglePlugin(plugin.plugin_id, plugin.enabled)}
                    className={`px-4 py-2 rounded-lg text-sm font-medium ${
                      plugin.enabled
                        ? 'bg-green-600 hover:bg-green-700 text-white'
                        : 'bg-gray-700 hover:bg-gray-600 text-gray-300'
                    }`}
                  >
                    {plugin.enabled ? 'Enabled' : 'Disabled'}
                  </button>
                  <button
                    onClick={() => uninstallPlugin(plugin.plugin_id)}
                    className="px-4 py-2 bg-red-600/20 hover:bg-red-600/40 text-red-400 rounded-lg text-sm"
                  >
                    Uninstall
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
