from alembic import op

def upgrade():
    # exemplo: adicionar estoque_bling em produtos
    op.execute("ALTER TABLE produtos ADD COLUMN estoque_bling INTEGER DEFAULT 0;")

def downgrade():
    # SQLite não remove coluna fácil; normalmente deixa vazio
    pass