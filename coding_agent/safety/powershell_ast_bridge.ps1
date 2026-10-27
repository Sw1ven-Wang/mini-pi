$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

while (($encoded = [Console]::In.ReadLine()) -ne $null) {
    try {
        $source = [System.Text.Encoding]::UTF8.GetString(
            [System.Convert]::FromBase64String($encoded)
        )
        $tokens = $null
        $parseErrors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseInput(
            $source,
            [ref]$tokens,
            [ref]$parseErrors
        )

        $commands = @()
        foreach ($node in $ast.FindAll({
            param($candidate)
            $candidate -is [System.Management.Automation.Language.CommandAst]
        }, $true)) {
            $elements = @($node.CommandElements | ForEach-Object {
                $_.Extent.Text
            })
            $redirects = @($node.Redirections | ForEach-Object {
                $target = $null
                if ($_.Location) { $target = $_.Location.Extent.Text }
                [ordered]@{
                    text = $_.Extent.Text
                    target = $target
                    start = $_.Extent.StartOffset
                    end = $_.Extent.EndOffset
                }
            })
            $depth = 0
            $parent = $node.Parent
            while ($null -ne $parent) {
                if (
                    $parent -is [System.Management.Automation.Language.SubExpressionAst] -or
                    $parent -is [System.Management.Automation.Language.ScriptBlockExpressionAst] -or
                    $parent -is [System.Management.Automation.Language.ExpandableStringExpressionAst]
                ) { $depth += 1 }
                $parent = $parent.Parent
            }
            $commands += [ordered]@{
                name = $node.GetCommandName()
                elements = $elements
                redirects = $redirects
                start = $node.Extent.StartOffset
                end = $node.Extent.EndOffset
                depth = $depth
            }
        }

        $assignments = @($ast.FindAll({
            param($candidate)
            $candidate -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $candidate.Left.Extent.Text -match '^\$env:'
        }, $true) | ForEach-Object {
            [ordered]@{
                left = $_.Left.Extent.Text
                right = $_.Right.Extent.Text
                start = $_.Extent.StartOffset
                end = $_.Extent.EndOffset
            }
        })

        $operatorKinds = @(
            "AndAnd", "OrOr", "Semi", "Pipe", "Ampersand", "NewLine"
        )
        $operators = @($tokens | Where-Object {
            $operatorKinds -contains $_.Kind.ToString()
        } | ForEach-Object { $_.Text })

        $payload = [ordered]@{
            commands = $commands
            assignments = $assignments
            operators = $operators
            errors = @($parseErrors | ForEach-Object { $_.Message })
        } | ConvertTo-Json -Depth 10 -Compress
    } catch {
        $payload = [ordered]@{
            commands = @()
            assignments = @()
            operators = @()
            errors = @($_.Exception.Message)
        } | ConvertTo-Json -Depth 4 -Compress
    }
    [Console]::Out.WriteLine(
        [System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($payload))
    )
    [Console]::Out.Flush()
}

