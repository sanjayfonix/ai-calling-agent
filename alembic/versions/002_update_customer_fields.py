"""Update customer fields - remove doctor/meds, add DOB/phone/household

Revision ID: 002_update_customer_fields
Revises: 001_add_address
Create Date: 2026-03-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '002_update_customer_fields'
down_revision: Union[str, None] = '001_add_address'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Remove old fields
    op.drop_column('customer_data', 'age')
    op.drop_column('customer_data', 'doctor_name')
    op.drop_column('customer_data', 'doctor_specialty')
    op.drop_column('customer_data', 'medicines')
    
    # Add new fields
    op.add_column('customer_data', sa.Column('date_of_birth', sa.String(20), nullable=True))
    op.add_column('customer_data', sa.Column('phone_number', sa.String(20), nullable=True))
    op.add_column('customer_data', sa.Column('tax_household_size', sa.Integer(), nullable=True))


def downgrade() -> None:
    # Remove new fields
    op.drop_column('customer_data', 'date_of_birth')
    op.drop_column('customer_data', 'phone_number')
    op.drop_column('customer_data', 'tax_household_size')
    
    # Restore old fields
    op.add_column('customer_data', sa.Column('age', sa.Integer(), nullable=True))
    op.add_column('customer_data', sa.Column('doctor_name', sa.Text(), nullable=True))
    op.add_column('customer_data', sa.Column('doctor_specialty', sa.Text(), nullable=True))
    op.add_column('customer_data', sa.Column('medicines', sa.Text(), nullable=True))
