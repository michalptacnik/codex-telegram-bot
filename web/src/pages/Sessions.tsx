import { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi';

interface Session {
  session_id: string;
  chat_id: string;
  user_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  last_message_at?: string;
  summary: string;
  message_count: number;
}

const statusColors: Record<string, string> = {
  active: 'bg-green-600',
  idle: 'bg-yellow-600',
  archived: 'bg-gray-600',
};

export default function Sessions() {
  const { fetchApi } = useApi();
  const [sessions, setSessions] = useState<Session[]>([]);

  useEffect(() => {
    fetchApi('/api/sessions').then((data) => {
      if (data?.sessions) setSessions(data.sessions);
    });
  }, []);

  return (
    <div className="p-6 max-w-6xl">
      <h1 className="text-2xl font-bold text-white mb-6">Sessions</h1>

      {sessions.length === 0 ? (
        <div className="bg-gray-900 rounded-xl p-8 border border-gray-800 text-center text-gray-400">
          No active sessions.
        </div>
      ) : (
        <div className="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="border-b border-gray-800">
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Session</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Status</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Messages</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Last Activity</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {sessions.map((session) => (
                <tr key={session.session_id} className="hover:bg-gray-800/50">
                  <td className="px-6 py-4">
                    <div className="text-white font-medium">{session.session_id}</div>
                    <div className="text-sm text-gray-400">
                      Chat: {session.chat_id} | User: {session.user_id}
                    </div>
                    {session.summary && (
                      <div className="text-sm text-gray-500 mt-1">{session.summary}</div>
                    )}
                  </td>
                  <td className="px-6 py-4">
                    <span className={`px-2 py-1 rounded-full text-xs font-medium text-white ${statusColors[session.status] || 'bg-gray-600'}`}>
                      {session.status}
                    </span>
                  </td>
                  <td className="px-6 py-4 text-gray-300">{session.message_count}</td>
                  <td className="px-6 py-4 text-gray-400 text-sm">
                    {session.last_message_at
                      ? new Date(session.last_message_at).toLocaleString()
                      : 'Never'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
