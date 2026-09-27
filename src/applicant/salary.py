"""Kept so `from applicant.salary import ...` goes on working; see `applicant.domain.salary`."""

from .domain.salary import Salary, parse_salary

__all__ = ['Salary', 'parse_salary']
