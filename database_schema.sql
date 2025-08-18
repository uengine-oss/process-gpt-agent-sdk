-- Supabase Database Schema for ProcessGPT Agent Framework

-- TodoList Table: 에이전트가 처리해야 할 작업들을 저장
CREATE TABLE todolist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_type VARCHAR(100) NOT NULL,
    prompt TEXT NOT NULL,
    input_data JSONB,
    agent_status VARCHAR(50) DEFAULT 'pending' CHECK (agent_status IN ('pending', 'in_progress', 'completed', 'failed', 'cancelled')),
    agent_output JSONB,
    priority INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

-- Events Table: 각 태스크의 실행 상태를 추적
CREATE TABLE events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    todolist_id UUID NOT NULL REFERENCES todolist(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN ('task_created', 'task_started', 'task_progress', 'task_completed', 'task_failed', 'task_cancelled')),
    event_data JSONB NOT NULL,
    context_id VARCHAR(255),
    task_id VARCHAR(255),
    message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for better performance
CREATE INDEX idx_todolist_agent_type_status ON todolist(agent_type, agent_status);
CREATE INDEX idx_todolist_created_at ON todolist(created_at);
CREATE INDEX idx_events_todolist_id ON events(todolist_id);
CREATE INDEX idx_events_created_at ON events(created_at);

-- Enable RLS (Row Level Security)
ALTER TABLE todolist ENABLE ROW LEVEL SECURITY;
ALTER TABLE events ENABLE ROW LEVEL SECURITY;

-- Basic policies (adjust based on your authentication needs)
CREATE POLICY "Enable read access for all users" ON todolist FOR SELECT USING (true);
CREATE POLICY "Enable insert access for all users" ON todolist FOR INSERT WITH CHECK (true);
CREATE POLICY "Enable update access for all users" ON todolist FOR UPDATE USING (true);
CREATE POLICY "Enable delete access for all users" ON todolist FOR DELETE USING (true);

CREATE POLICY "Enable read access for all users" ON events FOR SELECT USING (true);
CREATE POLICY "Enable insert access for all users" ON events FOR INSERT WITH CHECK (true);
CREATE POLICY "Enable update access for all users" ON events FOR UPDATE USING (true);
CREATE POLICY "Enable delete access for all users" ON events FOR DELETE USING (true);

-- Function to automatically update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger for todolist updated_at
CREATE TRIGGER update_todolist_updated_at 
    BEFORE UPDATE ON todolist 
    FOR EACH ROW 
    EXECUTE FUNCTION update_updated_at_column(); 