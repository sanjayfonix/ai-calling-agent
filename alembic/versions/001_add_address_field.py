"""Add address field to customer_data

Revision ID: 001_add_address
Revises: 
Create Date: 2026-03-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '001_add_address'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add address column to customer_data table
    op.add_column('customer_data', sa.Column('address', sa.Text(), nullable=True))


def downgrade() -> None:
    # Remove address column
    op.drop_column('customer_data', 'address')
