from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import ConflictError, NotFoundError
from app.models import Member
from app.schemas import ErrorResponse, MemberCreate, MemberOut

router = APIRouter(prefix="/members", tags=["Members"])


@router.post("", response_model=MemberOut, status_code=201, summary="Create a member",
             description="The email is only an identifier in this mock system. No email is ever sent.",
             responses={409: {"model": ErrorResponse, "description": "Email already registered."}})
def create_member(payload: MemberCreate, db: Session = Depends(get_db)) -> Member:
    try:
        with db.begin():
            if db.scalar(select(Member.id).where(Member.email == payload.email)) is not None:
                raise ConflictError("A member with this email already exists.", "email_taken")
            member = Member(name=payload.name, email=payload.email)
            db.add(member)
            db.flush()
    except IntegrityError as exc:
        raise ConflictError("A member with this email already exists.", "email_taken") from exc
    return member


@router.get("", response_model=list[MemberOut], summary="List members")
def list_members(db: Session = Depends(get_db)) -> list[Member]:
    with db.begin():
        return list(db.execute(select(Member).order_by(Member.id)).scalars())


@router.get("/{member_id}", response_model=MemberOut, summary="View a member",
            responses={404: {"model": ErrorResponse}})
def get_member(member_id: int, db: Session = Depends(get_db)) -> Member:
    with db.begin():
        member = db.get(Member, member_id)
        if member is None:
            raise NotFoundError(f"Member {member_id} does not exist.", "member_not_found")
    return member
