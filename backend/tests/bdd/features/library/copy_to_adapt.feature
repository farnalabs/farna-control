Feature: Copy to Adapt Library Primitives
  As a pipeline author
  I want to adapt local library primitives to my org and teams
  So that I can reuse and customise proven workflows

  Background:
    Given the organisation has 3 local primitives

  Scenario: Copy non-existent primitive returns 404
    When the user sends POST /api/v1/libraries/00000000-0000-0000-0000-00000000ffff/adapt
    Then the response status is 404
