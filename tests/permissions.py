"""Independent native ACL checks on Windows; exact mode-bit checks on POSIX."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from unittest import TestCase


def _powershell(path: str | Path, script: str) -> str:
    result = subprocess.run(
        [
            'powershell.exe',
            '-NoProfile',
            '-NonInteractive',
            '-Command',
            "$ErrorActionPreference = 'Stop'; " + script,
        ],
        # A pwsh parent can export module paths incompatible with Windows
        # PowerShell 5. Let the child construct its own native module paths.
        env={
            **{key: value for key, value in os.environ.items() if key.upper() != 'PSMODULEPATH'},
            'APPLICANT_TEST_ACL_PATH': str(path),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise AssertionError(f'Native ACL inspection failed: {result.stderr}')
    return result.stdout.strip()


def policy(path: str | Path):
    if os.name != 'nt':
        return stat.S_IMODE(Path(path).stat().st_mode)
    return json.loads(
        _powershell(
            path,
            """
        $acl = Get-Acl -LiteralPath $env:APPLICANT_TEST_ACL_PATH
        $sidType = [System.Security.Principal.SecurityIdentifier]
        @{
            owner = $acl.GetOwner($sidType).Value
            protected = $acl.AreAccessRulesProtected
            rules = @($acl.GetAccessRules($true, $true, $sidType) | ForEach-Object {
                @{ sid = $_.IdentityReference.Value; type = [string]$_.AccessControlType;
                   rights = [int]$_.FileSystemRights; inherited = $_.IsInherited }
            })
        } | ConvertTo-Json -Depth 5 -Compress
    """,
        )
    )


def assert_private(test: TestCase, path: str | Path) -> None:
    permissions = policy(path)
    if os.name != 'nt':
        test.assertEqual(permissions, 0o600)
        return
    test.assertTrue(permissions['protected'], 'private ACL must disable inheritance')
    test.assertEqual(len(permissions['rules']), 1)
    rule = permissions['rules'][0]
    test.assertEqual(rule['sid'], permissions['owner'])
    test.assertEqual(rule['type'], 'Allow')
    test.assertFalse(rule['inherited'])
    test.assertEqual(rule['rights'] & 0x1F01FF, 0x1F01FF)  # FILE_ALL_ACCESS
    # Verify that the actual process owner can still read the private file.
    with open(path, 'rb') as handle:
        handle.read(1)


def loosen(path: str | Path) -> None:
    if os.name != 'nt':
        os.chmod(path, 0o644)
        return
    _powershell(
        path,
        """
        $acl = Get-Acl -LiteralPath $env:APPLICANT_TEST_ACL_PATH
        $everyone = [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($everyone, 'Read', 'Allow')
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $env:APPLICANT_TEST_ACL_PATH -AclObject $acl
    """,
    )
